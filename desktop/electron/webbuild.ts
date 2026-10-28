/**
 * 前端自动构建（桌面壳启动前置步骤）：
 *
 * 问题：桌面壳加载的是后端同源页面（web/dist 构建产物，见 backend/server.py 的
 * 静态挂载），而 web/src 改动后 dist 是旧的——改完前端忘了 `npm run build`，
 * 桌面端看到的就还是旧界面。
 *
 * 方案：启动时比对 web/src（及各配置文件）与 web/dist/index.html 的修改时间，
 * 源码更新 → 自动跑 `npm run build`，构建完成才拉后端。
 *
 * 为什么必须在**拉后端之前**：backend 在 import 时注册 /assets 静态挂载，
 * dist（或 assets 目录）当时不存在挂载就被跳过，后补构建出来的界面资源 404。
 *
 * 纪律（与后端"任何异常降级不阻断主流程"同款）：
 * - 不新鲜/构建失败/超时都只记日志后放行——UI 顶多用旧版，绝不挡启动；
 * - node_modules 缺失不代装（npm ci 是分钟级且要网络，超出壳的职责）；
 * - 构建有超时上限，防止 npm 卡死把启动拖成黑屏。
 */
import { spawn } from "node:child_process";
import { existsSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

/** 参与新鲜度判定的源（存在才纳入；web/public 是 vite 的静态拷贝源） */
const SRC_ENTRIES = ["src", "public", "index.html", "package.json",
  "vite.config.ts", "vite.config.js", "vite.config.mts", "tsconfig.json"];

const BUILD_TIMEOUT_MS = 240_000;
const SKEW_GUARD_MS = 2_000;   // mtime 时钟毛刺容忍

/** 递归取目录内最新 mtime（有文件数上限，防 node_modules 之类的意外膨胀拖慢启动） */
function newestMtime(dir: string, budget: { left: number }): number {
  let newest = 0;
  let entries: string[];
  try {
    entries = readdirSync(dir);
  } catch {
    return 0;
  }
  for (const name of entries) {
    if (budget.left <= 0) return newest;
    if (name === "node_modules" || name === "dist" || name.startsWith(".")) continue;
    const full = join(dir, name);
    let st;
    try {
      st = statSync(full);
    } catch {
      continue;
    }
    budget.left -= 1;
    if (st.isDirectory()) {
      newest = Math.max(newest, newestMtime(full, budget));
    } else {
      newest = Math.max(newest, st.mtimeMs);
    }
  }
  return newest;
}

/** 前端是否需要重新构建：dist 缺失，或任一源比 dist/index.html 新 */
export function webBuildNeeded(root: string): boolean {
  const web = join(root, "web");
  const marker = join(web, "dist", "index.html");
  if (!existsSync(marker)) return true;
  let distMtime = 0;
  try {
    distMtime = statSync(marker).mtimeMs;
  } catch {
    return true;
  }
  const budget = { left: 4000 };
  let newest = 0;
  for (const entry of SRC_ENTRIES) {
    const p = join(web, entry);
    if (!existsSync(p)) continue;
    const st = statSync(p);
    if (st.isDirectory()) {
      newest = Math.max(newest, newestMtime(p, budget));
    } else {
      newest = Math.max(newest, st.mtimeMs);
    }
  }
  return newest > distMtime + SKEW_GUARD_MS;
}

/** 干净环境（与后端 spawn 同款：剥代理、本地直连、UTF-8 输出） */
function buildEnv(): NodeJS.ProcessEnv {
  const env = { ...process.env };
  for (const k of ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
    "ALL_PROXY", "all_proxy"]) {
    delete env[k];
  }
  env.NO_PROXY = "127.0.0.1,localhost";
  env.no_proxy = "127.0.0.1,localhost";
  env.PYTHONIOENCODING = "utf-8";
  return env;
}

/**
 * 需要时构建前端；完成（或放弃）后返回，**绝不抛异常**——调用方接着拉后端。
 * onStatus 驱动 loading 页文案。
 */
export async function ensureWebBuild(
  root: string,
  onStatus?: (msg: string) => void,
): Promise<void> {
  const web = join(root, "web");
  try {
    if (!existsSync(join(web, "package.json"))) {
      return; // 非 monorepo 布局（老包）：无前端可构建
    }
    if (!webBuildNeeded(root)) {
      return;
    }
    if (!existsSync(join(web, "node_modules"))) {
      onStatus?.("前端依赖未安装，跳过自动构建（需先 cd web && npm install）");
      return;
    }
    onStatus?.("检测到前端更新，正在构建 web/dist…");
    // Windows 下 npm 是 npm.cmd：Node ≥20.12 出于安全（CVE-2024-27980）拒绝
    // 直接 spawn .cmd，必须 shell:true（参数是固定常量，无注入面）。
    const useShell = process.platform === "win32";
    await new Promise<void>((resolve) => {
      const child = spawn("npm", ["run", "build"], {
        cwd: web,
        env: buildEnv(),
        windowsHide: true,
        shell: useShell,
        stdio: ["ignore", "pipe", "pipe"],
      });
      let done = false;
      const finish = (why: string) => {
        if (done) return;
        done = true;
        onStatus?.(why);
        resolve();
      };
      const timer = setTimeout(() => {
        try { child.kill(); } catch { /* 已退出 */ }
        finish("前端构建超时，先用旧版界面进入");
      }, BUILD_TIMEOUT_MS);
      child.stdout?.on("data", () => { /* vite 输出量大，只等结果不逐行转发 */ });
      child.stderr?.on("data", () => { /* 同上；失败原因由退出码体现 */ });
      child.once("exit", (code) => {
        clearTimeout(timer);
        finish(code === 0
          ? "前端构建完成"
          : `前端构建失败(code=${code})，先用旧版界面进入`);
      });
      child.once("error", () => {
        clearTimeout(timer);
        finish("前端构建无法启动（npm 不可用？），先用旧版界面进入");
      });
    });
  } catch (err) {
    // 任何意外都不挡启动：UI 顶多用旧版
    onStatus?.(`前端自动构建跳过：${err instanceof Error ? err.message : String(err)}`);
  }
}
