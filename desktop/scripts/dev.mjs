/**
 * 桌面壳开发模式一键启动：vite(web/) + 后端(uvicorn) + Electron 三件套。
 *
 * - vite:      web/ 的 npm run dev（5173，热更新）
 * - 后端:      127.0.0.1:8000/health 已就绪则复用（比如 start_all.bat 在跑），
 *              否则用仓库 .venv 拉一个
 * - Electron:  OMNI_DESKTOP_DEV=1，窗口加载 http://localhost:5173
 *              （vite 把 /api、/v1 代理到 8000）
 *
 * Ctrl+C 退出本脚本即可（Windows 控制台会向整个进程组发中断）。
 */
import { spawn } from "node:child_process";
import http from "node:http";
import { createServer } from "node:net";
import { existsSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const desktopDir = join(dirname(fileURLToPath(import.meta.url)), "..");
const root = join(desktopDir, "..");
const webDir = join(root, "web");
const python = process.platform === "win32"
  ? join(root, ".venv", "Scripts", "python.exe")
  : join(root, ".venv", "bin", "python");
const electronExe = join(desktopDir, "node_modules", "electron", "dist", "electron.exe");

const children = [];

function run(cmd, args, opts, name) {
  console.log(`[dev] 启动 ${name}: ${cmd} ${args.join(" ")}`);
  const child = spawn(cmd, args, { ...opts, stdio: ["ignore", "pipe", "pipe"], windowsHide: true });
  const tag = `[${name}]`;
  const pump = (chunk) => {
    for (const line of chunk.toString().split(/\r?\n/)) {
      if (line.trim()) console.log(`${tag} ${line}`);
    }
  };
  child.stdout.on("data", pump);
  child.stderr.on("data", pump);
  child.on("exit", (code) => console.log(`${tag} 退出 code=${code}`));
  children.push(child);
  return child;
}

function httpOk(url) {
  return new Promise((resolve) => {
    const req = http.get(url, { timeout: 1500 }, (res) => {
      res.resume();
      resolve(res.statusCode < 500);
    });
    req.on("timeout", () => { req.destroy(); resolve(false); });
    req.on("error", () => resolve(false));
  });
}

function portFree(port) {
  return new Promise((resolve) => {
    const srv = createServer();
    srv.once("error", () => resolve(false));
    srv.once("listening", () => srv.close(() => resolve(true)));
    srv.listen(port, "127.0.0.1");
  });
}

async function waitReady(url, ms, label) {
  const deadline = Date.now() + ms;
  process.stdout.write(`[dev] 等待 ${label} 就绪 ${url} `);
  while (Date.now() < deadline) {
    if (await httpOk(url)) {
      console.log("✓");
      return true;
    }
    process.stdout.write(".");
    await new Promise((r) => setTimeout(r, 500));
  }
  console.log("✗ 超时");
  return false;
}

function cleanup() {
  for (const c of children) {
    try { c.kill(); } catch { /* 已退出 */ }
  }
  process.exit(0);
}
process.on("SIGINT", cleanup);
process.on("SIGTERM", cleanup);

// 1) vite（5173 被占说明已在跑，直接复用）
if (await portFree(5173)) {
  // .cmd 必须带 shell:true（Node ≥18.20 禁止无 shell 直接 spawn 批处理，EINVAL）
  run(process.platform === "win32" ? "npm.cmd" : "npm", ["run", "dev"],
    { cwd: webDir, shell: process.platform === "win32" }, "vite");
}
if (!(await waitReady("http://localhost:5173", 30_000, "vite"))) {
  console.error("[dev] vite 未能就绪，退出");
  process.exit(1);
}

// 2) 后端（8000 有健康服务就复用，否则拉起）
if (!(await httpOk("http://127.0.0.1:8000/health"))) {
  if (!existsSync(python)) {
    console.error(`[dev] 未找到 ${python}，请先按 README 创建 .venv`);
    process.exit(1);
  }
  const env = { ...process.env };
  delete env.HTTP_PROXY; delete env.HTTPS_PROXY; delete env.http_proxy; delete env.https_proxy;
  env.NO_PROXY = "127.0.0.1,localhost";
  env.PYTHONIOENCODING = "utf-8";
  run(python, ["-m", "uvicorn", "backend.server:app", "--host", "127.0.0.1", "--port", "8000"],
    { cwd: root, env }, "backend");
}
if (!(await waitReady("http://127.0.0.1:8000/health", 90_000, "后端"))) {
  console.error("[dev] 后端未能就绪，退出");
  process.exit(1);
}

// 3) Electron（窗口加载 5173，壳不做进程管理）
if (!existsSync(electronExe)) {
  console.error(`[dev] 未找到 ${electronExe}，请先在 desktop/ 执行 npm install`);
  process.exit(1);
}
run(electronExe, ["."], { cwd: desktopDir, env: { ...process.env, OMNI_DESKTOP_DEV: "1" } }, "electron");
console.log("[dev] 全部就绪。Ctrl+C 退出全部进程。");
