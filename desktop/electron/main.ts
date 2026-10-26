/**
 * OmniAgent 桌面壳主进程。
 *
 * 职责：单实例锁 → 建窗（loading 页）→ 拉起后端（.venv uvicorn）→ 等健康检查
 * → 加载后端同源承载的 Web UI；托盘常驻（关窗隐藏）、后端崩溃自动重启
 * （≤3 次退避）、退出时 taskkill 杀后端进程树。
 *
 * 开发模式（OMNI_DESKTOP_DEV=1，由 scripts/dev.mjs 注入）：直接加载 vite 5173，
 * vite 与后端均由 dev.mjs 管理，壳不做进程管理。
 */
import { app, BrowserWindow, dialog, globalShortcut, ipcMain, shell, WebContentsView } from "electron";
import { join } from "node:path";
import { dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { loadDesktopConfig, type DesktopConfig } from "./config";
import {
  findFreePort,
  killBackendTree,
  logsDir,
  shellLog,
  startBackend,
  startGateway,
  waitForHealth,
  type BackendHandle,
} from "./backend";
import type { ChildProcess } from "node:child_process";
import { projectRootError, resolveProjectRoot } from "./project";
import { createTray, disposeTray, setTrayStatus } from "./tray";

const IS_DEV = process.env.OMNI_DESKTOP_DEV === "1";
const HEALTH_TIMEOUT_MS = 90_000; // 后端首启要导入 opencv/onnxruntime，超时放宽
const MAX_RESTARTS = 3;

// 无边框模式（对齐 WorkBuddy 布局）：壳自有导航条 + 内容视图两层——
// - 主 webContents 装 resources/titlebar.html（#2e2e2e 与侧栏同色，整条可拖，
//   右侧原生三键由系统 titleBarOverlay 绘制在同色条上，无左右色差）
// - web UI 装进 WebContentsView，渲染在导航条正下方——前端零改动，
//   侧栏 "OmniAgent" 标题原样显示在导航条下方（不再被顶条吃掉）
// - 唯一的注入：把侧栏 MUI Toolbar（默认 min-height 56/64px 垂直居中，
//   标题上下有大片空白）压成与导航条等高的一行，标题上方间距≈0
// - frameless=false 时回退系统原生边框，导航条不显示、内容铺满全窗
const TITLEBAR = { height: 29, color: "#2e2e2e", symbolColor: "#cfd8dc" };

// Electron ESM 主进程没有 __dirname；import.meta.url 经 esbuild 打包后指向
// dist-electron/main.mjs，派生目录即壳产物目录（preload 同级、resources 在上一级）
const APP_DIR = dirname(fileURLToPath(import.meta.url));

// dev 与打包统一 userData（logs、单实例锁都在 %APPDATA%/OmniAgent）；
// OMNI_USER_DATA 可隔离出并行实例（多开测试/调试用）
app.setPath(
  "userData",
  process.env.OMNI_USER_DATA?.trim() || join(app.getPath("appData"), "OmniAgent"),
);

let win: BrowserWindow | null = null;
/** web UI 容器（导航条下方的内容视图） */
let appView: WebContentsView | null = null;
/** 侧栏收起态（导航条「收起侧边栏」按钮） */
let sidebarCollapsed = false;
let backend: BackendHandle | null = null;
/** 网关进程（cron 调度 + 消息通道），随应用生命周期 */
let gateway: ChildProcess | null = null;
let quitting = false;
let restarts = 0;
let cfg: DesktopConfig;

// ── 窗口 ──────────────────────────────────────────────────

async function createWindow(): Promise<void> {
  win = new BrowserWindow({
    width: cfg.window.width,
    height: cfg.window.height,
    minWidth: cfg.window.min_width,
    minHeight: cfg.window.min_height,
    title: "OmniAgent",
    show: false,
    autoHideMenuBar: true,
    backgroundColor: TITLEBAR.color,
    ...(cfg.frameless
      ? {
          titleBarStyle: "hidden" as const,
          titleBarOverlay: {
            color: TITLEBAR.color,
            symbolColor: TITLEBAR.symbolColor,
            height: TITLEBAR.height,
          },
        }
      : {}),
    webPreferences: {
      preload: join(APP_DIR, "titlebar-preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      spellcheck: false,
    },
  });

  // 内容视图：承载 web UI（loading → 后端同源页面 / dev 的 vite）
  appView = new WebContentsView({
    webPreferences: {
      preload: join(APP_DIR, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      spellcheck: false,
    },
  });
  win.contentView.addChildView(appView);
  const layoutViews = () => {
    if (!win || !appView) return;
    const [w, h] = win.getContentSize();
    const top = cfg.frameless ? TITLEBAR.height : 0;
    appView.setBounds({ x: 0, y: top, width: w, height: Math.max(0, h - top) });
  };
  win.on("resize", layoutViews);
  layoutViews();

  if (cfg.frameless) {
    await win.loadFile(join(APP_DIR, "..", "resources", "titlebar.html"));
  }
  await appView.webContents.loadFile(join(APP_DIR, "..", "resources", "loading.html"));
  // ready-to-show 在「主 webContents=导航条 + 子内容视图」结构下不可靠（不触发则窗口
  // 永不显示）；两个页面 load 完成后直接显示，背景色已同色无白闪。
  win.show();
  win.on("close", (event) => {
    if (quitting) return;
    if (cfg.show_tray) {
      event.preventDefault();
      win?.hide();
    }
    // show_tray=false：不拦截，走默认关闭 → window-all-closed → app.quit()
  });
  appView.webContents.on("before-input-event", (_e, input) => {
    if (input.type === "keyDown" && input.key === "F12") win?.webContents.toggleDevTools();
  });
  // 对 web UI 的全部微调（仅无边框模式，四条规则，其余零注入）：
  // 1) 侧栏 MUI Toolbar 默认 min-height 56/64px + 垂直居中 → 标题上方一大块空白；
  //    压成与导航条等高的一行（标题上方间距≈0）
  // 2) 圆角面板：body 底色取导航条色 #2e2e2e（只在 main 顶部圆角缺口处露出）；
  //    main 自身显式上 #121212（theme background.default，web 原生色）——
  //    聊天/设置/技能等所有页面都在 main 内，底色与 web 版完全一致，
  //    不会因全局底色被改而串色
  // 3) 侧栏收起：宽度过渡动画（0.18s）而非 display:none 瞬间消失——
  //    Drawer 根节点与 paper 都锁 260px 基准宽，收起态宽度归零 + overflow hidden 裁切
  appView.webContents.on("did-finish-load", () => {
    if (!cfg.frameless || !appView) return;
    void appView.webContents
      .insertCSS(
        `.MuiDrawer-paper .MuiToolbar-root{min-height:0!important;height:${TITLEBAR.height}px!important;}` +
          `body{background:#2e2e2e!important;}` +
          `#root main{background:#121212!important;border-radius:12px 12px 0 0!important;}` +
          `.MuiDrawer-root,.MuiDrawer-paper{transition:width .18s ease!important;` +
          `width:260px!important;overflow:hidden!important;}` +
          `body.omni-sidebar-collapsed .MuiDrawer-root,` +
          `body.omni-sidebar-collapsed .MuiDrawer-paper{width:0!important;}`,
        { cssOrigin: "user" },
      )
      .catch(() => {});
    // 恢复收起态（后端重启重载页面后保持）
    if (sidebarCollapsed) {
      void appView.webContents
        .executeJavaScript(`document.body.classList.add('omni-sidebar-collapsed')`)
        .catch(() => {});
    }
  });
  win.on("closed", () => {
    win = null;
  });
}

function showWindow(): void {
  if (!win) {
    void createWindow();
    return;
  }
  if (win.isMinimized()) win.restore();
  win.show();
  win.focus();
}

// ── 导航条 IPC（titlebar 按钮 → 操控内容视图） ─────────────

ipcMain.handle("nav:toggle-sidebar", () => {
  sidebarCollapsed = !sidebarCollapsed;
  appView?.webContents
    .executeJavaScript(`document.body.classList.toggle('omni-sidebar-collapsed', ${sidebarCollapsed})`)
    .catch(() => {});
  // 同步导航条：「新建任务」按钮只在侧栏收起后出现（侧栏自带新建入口，避免双入口）
  if (cfg?.frameless) {
    execJs(win?.webContents, `window.setSidebarState && window.setSidebarState(${sidebarCollapsed})`);
  }
  return sidebarCollapsed;
});

ipcMain.handle("nav:new-task", () => {
  // 代点 web UI 自己的「新建对话」入口——selectTask("")+setView("chat") 的
  // 逻辑由前端自己执行，壳不重复实现业务
  return (
    appView?.webContents
      .executeJavaScript(`(function () {
        var els = document.querySelectorAll('[role="button"]');
        for (var i = 0; i < els.length; i++) {
          if ((els[i].textContent || '').trim() === '新建对话') { els[i].click(); return true; }
        }
        return false;
      })()`)
      .catch(() => false) ?? Promise.resolve(false)
  );
});

// ── 状态注入：导航条常驻状态 + loading 页大状态 ───────────

function execJs(wc: { executeJavaScript: (code: string) => Promise<unknown> } | undefined | null, code: string): void {
  if (!wc) return;
  wc.executeJavaScript(code).catch(() => {});
}

function setStatus(text: string): void {
  // 导航条状态（常驻，运行中显示端口）
  if (cfg?.frameless) {
    execJs(win?.webContents, `window.setStatus && window.setStatus(${JSON.stringify(text)})`);
  }
  // loading 页状态（仅当内容视图还停在启动页时）
  const url = appView?.webContents.getURL() ?? "";
  if (url.startsWith("file://")) {
    execJs(appView?.webContents, `window.setStatus && window.setStatus(${JSON.stringify(text)})`);
  }
}

function showErrorState(err: Error): void {
  const logPath = join(logsDir(), "backend.log");
  const msg = `${err.message}\n\n后端日志：${logPath}`;
  if (cfg?.frameless) {
    execJs(win?.webContents, `window.setStatus && window.setStatus(${JSON.stringify(err.message.slice(0, 60))}, true)`);
  }
  execJs(appView?.webContents, `window.showError && window.showError(${JSON.stringify(msg)})`);
  setTrayStatus(`异常：${err.message.slice(0, 60)}`);
  dialog.showErrorBox("OmniAgent 启动失败", msg);
}

// ── 后端生命周期 ──────────────────────────────────────────

/** 拉起网关并挂自愈链：每次重启的实例都要重新挂 exit 监听（链才不断） */
function spawnGatewayWithRestart(root: string): void {
  if (quitting) return;
  gateway = startGateway(root);
  gateway.once("exit", (code) => {
    if (quitting) return;
    shellLog(`gateway 退出(code=${code})，5s 后自动重启`);
    setTimeout(() => {
      if (!quitting && backend) spawnGatewayWithRestart(root);
    }, 5000);
  });
}

async function ensureBackend(): Promise<void> {
  try {
    const root = resolveProjectRoot();
    if (backend) {
      const old = backend;
      backend = null;
      old.process.removeAllListeners("exit");
      await killBackendTree(old.process.pid);
    }
    const port = await findFreePort(8000);
    setStatus(`正在启动后端（127.0.0.1:${port}）…`);
    setTrayStatus("后端启动中…");
    const handle = startBackend(root, port);
    backend = handle;
    handle.process.once("exit", onBackendExit);
    // 网关与后端并列常驻：cron 定时触发 + 飞书通道（凭据缺失时网关自身降级为只跑调度器）。
    // 守卫：手动「重启后端」/ 崩溃自愈重入时避免拉出第二个网关（scheduler 会双触发）。
    if (!gateway || gateway.exitCode !== null) {
      spawnGatewayWithRestart(root);
    }
    await waitForHealth(port, HEALTH_TIMEOUT_MS, (s) => setStatus(`正在启动后端… ${s}s`));
    setStatus("后端就绪，加载界面…");
    setTrayStatus(`运行中 :${port}`);
    await appView?.webContents.loadURL(`http://127.0.0.1:${port}`);
    setStatus(`运行中 :${port}`);
    restarts = 0;
  } catch (err) {
    showErrorState(err instanceof Error ? err : new Error(String(err)));
  }
}

function onBackendExit(code: number | null): void {
  backend = null;
  if (quitting) return;
  restarts += 1;
  if (restarts > MAX_RESTARTS) {
    showErrorState(new Error(`后端连续异常退出 ${MAX_RESTARTS} 次，已停止自动重启`));
    setTrayStatus("后端已停止");
    return;
  }
  const delayS = 2 * restarts;
  setStatus(`后端退出（code=${code}），${delayS}s 后自动重启（第 ${restarts}/${MAX_RESTARTS} 次）…`);
  setTrayStatus("后端重启中…");
  setTimeout(() => {
    if (!quitting) void ensureBackend();
  }, delayS * 1000);
}

// ── 托盘 / 快捷键 ─────────────────────────────────────────

function setupTray(): void {
  if (!cfg.show_tray) return;
  createTray({
    showWindow: () => showWindow(),
    restartBackend: () => {
      restarts = 0;
      void ensureBackend();
    },
    openLogs: () => void shell.openPath(logsDir()),
    quit: () => app.quit(),
  });
}

function registerHotkey(): void {
  if (!cfg.hotkey) return;
  try {
    globalShortcut.register(cfg.hotkey, () => {
      if (win?.isVisible() && win.isFocused()) win.hide();
      else showWindow();
    });
  } catch {
    /* 注册失败不致命（v3 同款静默） */
  }
}

// ── 启动 / 退出 ───────────────────────────────────────────

async function bootstrap(): Promise<void> {
  // 项目根解析失败时窗口仍可用（显示错误），config 用默认值
  let root: string | null = null;
  try {
    root = resolveProjectRoot();
  } catch {
    /* showErrorState 在 ensureBackend 或下方统一报 */
  }
  cfg = loadDesktopConfig(root);
  try {
    await createWindow();
  } catch (err) {
    showErrorState(err instanceof Error ? err : new Error(String(err)));
    return;
  }
  setupTray();
  registerHotkey();

  if (IS_DEV) {
    setStatus("开发模式：加载 vite dev server…");
    await appView?.webContents.loadURL(process.env.OMNI_VITE_URL || "http://localhost:5173");
    setStatus("开发模式 :5173");
    setTrayStatus("开发模式 :5173");
    return;
  }
  if (!root) {
    showErrorState(new Error(projectRootError()));
    return;
  }
  await ensureBackend();
}

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on("second-instance", () => showWindow());
  app.setAppUserModelId("com.omniagent.desktop");
  app.whenReady().then(() => void bootstrap());
}

app.on("window-all-closed", () => {
  // 托盘模式常驻；关窗即退模式（show_tray=false）在此收尾
  if (!cfg?.show_tray) app.quit();
});

app.on("before-quit", (event) => {
  // 先拦下：杀完后端进程树再强制退出（v3 退出链路教训 + 进程树清理）
  event.preventDefault();
  quitting = true;
  void (async () => {
    try {
      const kills: Array<Promise<void>> = [];
      if (backend) {
        const pid = backend.process.pid;
        backend = null;
        kills.push(
          Promise.race([
            killBackendTree(pid),
            new Promise<void>((r) => setTimeout(r, 5000)),
          ]),
        );
      }
      if (gateway) {
        const gpid = gateway.pid;
        gateway = null;
        if (gpid) kills.push(killBackendTree(gpid));
      }
      await Promise.race([
        Promise.all(kills).then(() => undefined),
        new Promise<void>((r) => setTimeout(r, 6000)),
      ]);
    } finally {
      globalShortcut.unregisterAll();
      app.exit(0);
    }
  })();
});
