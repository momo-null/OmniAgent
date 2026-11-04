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
import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { loadDesktopConfig, voiceServiceConfig, type DesktopConfig } from "./config";
import {
  findFreePort,
  isPortFree,
  killBackendTree,
  logsDir,
  shellLog,
  startBackend,
  startGateway,
  startVoiceEngine,
  waitForHealth,
  type BackendHandle,
} from "./backend";
import type { ChildProcess } from "node:child_process";
import { projectRootError, resolveProjectRoot, resolveProjectRootSafe } from "./project";
import { ensureWebBuild } from "./webbuild";
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

// 「窗口可见但整片空白，拖一下才出画面」= Windows 上合成器停摆：原生遮挡检测误判（失焦/
// 多屏/RDP 下把可见窗口判成被遮挡 → 不再产帧，Chromium 上游 40825463 同款）、后台渲染器
// 被回收、省电模式节流，都会走到这个终态。一次 appendSwitch 关掉整组相关 feature：
// Chromium 对不认识的 feature 名直接忽略，多列几个不会报错。
// 代价是被遮挡的窗口仍继续渲染，CPU 略增——对常驻 agent 控制台可接受。
app.commandLine.appendSwitch(
  "disable-features",
  ["CalculateNativeWinOcclusion", "RendererBackgrounding", "HighEfficiencyModeEnabled"].join(","),
);

// 硬件加速开关必须在 app ready 前决定，而 cfg 要到 bootstrap（ready 之后）才读得到，
// 所以在模块顶层单独读一次（纯路径 + 文件 IO，不依赖 Electron 生命周期）
if (loadDesktopConfig(resolveProjectRootSafe()).software_render) {
  app.disableHardwareAcceleration();
}

let win: BrowserWindow | null = null;
/** web UI 容器（导航条下方的内容视图） */
let appView: WebContentsView | null = null;
/** 侧栏收起态（导航条「收起侧边栏」按钮） */
let sidebarCollapsed = false;
let backend: BackendHandle | null = null;
/** 网关进程（cron 调度 + 消息通道），随应用生命周期 */
let gateway: ChildProcess | null = null;
/** 语音引擎常驻服务（只在有语音请求时加载模型；空闲释放模型但不退进程） */
let voiceEngine: ChildProcess | null = null;
let quitting = false;
let restarts = 0;
let cfg: DesktopConfig;

// ── 窗口 ──────────────────────────────────────────────────

const layoutViews = (): void => {
  if (!win || !appView || win.isDestroyed()) return;
  const [w, h] = win.getContentSize();
  const top = cfg.frameless ? TITLEBAR.height : 0;
  appView.setBounds({ x: 0, y: top, width: w, height: Math.max(0, h - top) });
};

/**
 * 强制合成器重新出帧——「窗口可见但整片空白，拖一下才出画面」的自救路径。
 *
 * 只做 `webContents.invalidate()` 不够：它只是「请求一次重绘」，合成器/遮挡判定停摆时
 * 请求会被丢弃。用户拖动窗口之所以能救回来，是因为移动/尺寸变化会走一次完整的
 * WM_SIZE → 重排 → 强制产帧。这里用 bounds 抖动（改 1px 立刻改回，不做动画）程序化
 * 地复刻这个动作，最大化/最小化态下跳过（setBounds 在这两种态下语义不可靠）。
 */
function forceRepaint(): void {
  if (!win || win.isDestroyed() || !win.isVisible()) return;
  if (appView && !appView.webContents.isDestroyed()) appView.webContents.invalidate();
  if (win.isMaximized() || win.isMinimized() || win.isFullScreen()) return;
  const b = win.getBounds();
  win.setBounds({ x: b.x, y: b.y, width: b.width, height: b.height - 1 }, false);
  win.setBounds(b, false);
  layoutViews();
}

/**
 * 取证开关（默认关闭，OMNI_SHOT_ON_SHOW=1 才生效）：把内容视图当时的真实画面存成
 * logs/shot-&lt;tag&gt;.png。白屏只有像素能证明修好了——日志里一句「加载完成」说明不了
 * 合成器有没有出帧。capturePage 走独立的Surface快照，不影响正常渲染路径。
 */
function shotForDiagnostics(tag: string, delayMs = 300): void {
  if (!process.env.OMNI_SHOT_ON_SHOW) return;
  setTimeout(() => {
    const view = appView;
    if (!view || view.webContents.isDestroyed()) return;
    // DOM 侧探针：区分「页面根本没渲染（root 空）」与「渲染了但合成器没出帧」——
    // 两者的画面都是纯色，只有 DOM 数据能把它们分开。
    void view.webContents
      .executeJavaScript(
        `({rootChildren: (document.getElementById('root')||{}).childElementCount ?? -1,` +
          ` bodyText: (document.body?.innerText || '').slice(0, 40).replace(/\\s+/g, ' '),` +
          ` bodyBg: getComputedStyle(document.body).backgroundColor,` +
          ` painted: document.querySelector('#root')?.getBoundingClientRect().height ?? 0})`,
      )
      .catch(() => null)
      .then((dom) => {
        void view.webContents
          .capturePage()
          .then((img) => {
            const buf = img.toPNG();
            const url = view.webContents.getURL();
            const file = join(logsDir(), `shot-${tag}.png`);
            writeFileSync(file, buf);
            const { width, height } = img.getSize();
            shellLog(
              `诊断截图 ${tag}: url=${url} ${width}x${height} png=${buf.length} ` +
                `dom=${JSON.stringify(dom)} -> ${file}`,
            );
          })
          .catch((e: unknown) => shellLog(`诊断截图 ${tag} 失败: ${String(e)}`));
      });
  }, delayMs);
}

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
      // 常驻 agent 控制台：失焦/被遮挡时不节流定时器与绘制（SSE 流照常走，
      // 也堵住「后台节流停绘 → 空白」这条同症状路径）
      backgroundThrottling: false,
    },
  });
  // 未出帧时子视图的表面临时露出来的是默认底色（web contents 默认 #FFF 白）——
  // 「整片白屏」里那层白就是它。显式压成与导航条同色，缺帧时降级成同色底而非刺眼白。
  appView.setBackgroundColor(TITLEBAR.color);
  win.contentView.addChildView(appView);
  win.on("resize", layoutViews);
  layoutViews();

  if (cfg.frameless) {
    await win.loadFile(join(APP_DIR, "..", "resources", "titlebar.html"));
  }
  await appView.webContents.loadFile(join(APP_DIR, "..", "resources", "loading.html"));
  // ready-to-show 在「主 webContents=导航条 + 子内容视图」结构下不可靠（不触发则窗口
  // 永不显示）；两个页面 load 完成后直接显示，背景色已同色无白闪。
  win.show();
  // 合成器自救：窗口刚可见时最容易出现「有窗口没画面」（首帧还没被 DWM 拉起来）。
  // show/restore 走带抖动的完整自救；focus 太频繁，只做一次廉价的 invalidate 请求，
  // 避免每次切回来都抖 1px 尺寸。
  const repaintNow = () => {
    forceRepaint();
    setTimeout(forceRepaint, 150);
  };
  repaintNow();
  win.on("show", repaintNow);
  win.on("restore", repaintNow);
  win.on("focus", () => {
    if (appView && !appView.webContents.isDestroyed()) appView.webContents.invalidate();
  });
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
    // 页面加载完成是「白屏」最常暴露的时刻（真实 UI 取代 loading 页进来，但合成器没跟上）：
    // 立刻请求重绘，稍后再补一次完整自救
    if (appView && !appView.webContents.isDestroyed()) appView.webContents.invalidate();
    setTimeout(forceRepaint, 120);
    shotForDiagnostics("after-load");
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
  // 取证模式下的定点采样：不依赖加载事件，按秒打点，能看到「窗口显示后第几秒仍是同一帧」
  if (process.env.OMNI_SHOT_ON_SHOW) {
    for (const s of [1, 3, 6, 12]) shotForDiagnostics(`${s}s`, s * 1000);
  }
}

function showWindow(): void {
  if (!win) {
    void createWindow();
    return;
  }
  if (win.isMinimized()) win.restore();
  win.show();
  win.focus();
  // 已经可见时 show() 不再发 show 事件（例如 second-instance / 托盘点击落在可见窗口上），
  // 这里主动补一次完整自救——这正是用户「把窗口叫回来」的手势
  forceRepaint();
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
  // 后端端口可能是 findFreePort 拿到的非 8000，交给网关做事件通知的目标地址
  const backendUrl = backend ? `http://127.0.0.1:${backend.port}` : "";
  gateway = startGateway(root, backendUrl);
  gateway.once("exit", (code) => {
    if (quitting) return;
    shellLog(`gateway 退出(code=${code})，5s 后自动重启`);
    setTimeout(() => {
      if (!quitting && backend) spawnGatewayWithRestart(root);
    }, 5000);
  });
}

/**
 * 拉起语音引擎常驻服务（插件启用才起）。
 *
 * 端口已占用 = 已有实例在跑（手动起的 / 上一次没退干净），直接跳过而不是抢端口；
 * 进程崩了 5s 后重试——它自己不会因空闲退出（空闲只释放模型），所以不需要按需拉起。
 */
async function ensureVoiceEngine(root: string): Promise<void> {
  if (quitting || voiceEngine) return;
  const vc = voiceServiceConfig();
  if (!vc.enabled) {
    shellLog("voice 插件未启用，跳过语音引擎服务", "voice-engine.log");
    return;
  }
  if (!(await isPortFree(vc.port))) {
    shellLog(`语音引擎端口 ${vc.port} 已被占用，视为已有实例在运行`, "voice-engine.log");
    return;
  }
  voiceEngine = startVoiceEngine(root, vc.port);
  voiceEngine.once("exit", (code) => {
    voiceEngine = null;
    if (quitting) return;
    shellLog(`voice engine 退出(code=${code})，5s 后重启`, "voice-engine.log");
    setTimeout(() => {
      if (!quitting) void ensureVoiceEngine(root);
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
    // 前端自动构建（web/src 比 dist 新 → npm run build）。必须先于后端启动：
    // backend 在 import 时注册 /assets 静态挂载，dist 当时缺失就永远 404。
    // 失败/超时只降级用旧版界面，绝不挡启动。
    await ensureWebBuild(root, (m) => {
      setStatus(m);
      setTrayStatus(m.slice(0, 30));
    });
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
    // 语音引擎常驻服务：不阻塞界面（它启动只监听端口，不加载模型）
    void ensureVoiceEngine(root);
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
      if (voiceEngine) {
        const vpid = voiceEngine.pid;
        voiceEngine = null;
        if (vpid) kills.push(killBackendTree(vpid));
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
