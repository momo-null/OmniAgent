/**
 * 后端进程管理（v3 壳的空白区，此处全新实现）：
 * 端口探测 → spawn uvicorn（仓库 .venv 的 python）→ 轮询 /health → 退出时 taskkill 杀进程树。
 *
 * 约定对齐 start_all.bat：清代理环境变量、NO_PROXY 本地直连、PYTHONIOENCODING=utf-8；
 * 绝不带 --reload（Windows 下双进程抢绑端口会随机挂起，README 有警告）。
 */
import { spawn, type ChildProcess } from "node:child_process";
import { createServer } from "node:net";
import http from "node:http";
import {
  appendFileSync,
  existsSync,
  mkdirSync,
  renameSync,
  statSync,
  unlinkSync,
} from "node:fs";
import { join } from "node:path";
import { app } from "electron";
import { projectPython } from "./project";

export interface BackendHandle {
  process: ChildProcess;
  port: number;
  root: string;
}

// ── 端口 ──────────────────────────────────────────────────

function isPortFree(port: number): Promise<boolean> {
  return new Promise((resolve) => {
    const srv = createServer();
    srv.once("error", () => resolve(false));
    srv.once("listening", () => srv.close(() => resolve(true)));
    srv.listen(port, "127.0.0.1");
  });
}

/** 从 preferred 起向后找空闲端口；区间全占用则交给系统分配临时端口 */
export async function findFreePort(preferred: number, span = 40): Promise<number> {
  for (let p = preferred; p < preferred + span; p++) {
    if (await isPortFree(p)) return p;
  }
  return new Promise((resolve, reject) => {
    const srv = createServer();
    srv.once("error", reject);
    srv.listen(0, "127.0.0.1", () => {
      const addr = srv.address();
      const port = typeof addr === "object" && addr ? addr.port : preferred;
      srv.close(() => resolve(port));
    });
  });
}

// ── 日志 ──────────────────────────────────────────────────

export function logsDir(): string {
  const dir = join(app.getPath("userData"), "logs");
  mkdirSync(dir, { recursive: true });
  return dir;
}

function rotateLog(path: string, maxBytes = 5 * 1024 * 1024): void {
  try {
    if (existsSync(path) && statSync(path).size > maxBytes) {
      const old = `${path}.old`;
      if (existsSync(old)) unlinkSync(old);
      renameSync(path, old);
    }
  } catch {
    /* 轮转失败不影响主流程 */
  }
}

function appendLog(line: string, name = "backend.log"): void {
  try {
    const path = join(logsDir(), name);
    rotateLog(path);
    appendFileSync(path, line);
  } catch {
    /* 日志失败不影响主流程 */
  }
}

// ── 进程 ──────────────────────────────────────────────────

function cleanEnv(): NodeJS.ProcessEnv {
  const env = { ...process.env };
  for (const k of ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"]) {
    delete env[k];
  }
  env.NO_PROXY = "127.0.0.1,localhost";
  env.no_proxy = "127.0.0.1,localhost";
  env.PYTHONIOENCODING = "utf-8";
  return env;
}

export function startBackend(root: string, port: number): BackendHandle {
  const args = [
    "-m", "uvicorn", "backend.server:app",
    "--host", "127.0.0.1",
    "--port", String(port),
  ];
  const child = spawn(projectPython(root), args, {
    cwd: root,
    env: cleanEnv(),
    windowsHide: true,
    stdio: ["ignore", "pipe", "pipe"],
  });
  appendLog(`\n[shell] ${new Date().toISOString()} spawn uvicorn port=${port} pid=${child.pid}\n`);
  const pump = (chunk: Buffer) => appendLog(chunk.toString("utf8"));
  child.stdout?.on("data", pump);
  child.stderr?.on("data", pump);
  return { process: child, port, root };
}

/** 壳层事件日志（默认记网关侧；后端侧直接走 backend.log 的 pump） */
export function shellLog(msg: string, name = "gateway.log"): void {
  appendLog(`\n[shell] ${new Date().toISOString()} ${msg}\n`, name);
}

/**
 * 网关常驻（定时任务调度 + 飞书/企微通道）：桌面应用作为日常宿主一并拉起。
 * 凭据未配（如 FEISHU_APP_SECRET 缺失）时网关自身降级为只跑调度器，不影响使用；
 * 配好后重启应用即可连上通道。
 */
export function startGateway(root: string): ChildProcess {
  const child = spawn(projectPython(root), ["-m", "gateway.channel_gateway"], {
    cwd: root,
    env: cleanEnv(),
    windowsHide: true,
    stdio: ["ignore", "pipe", "pipe"],
  });
  appendLog(`\n[shell] ${new Date().toISOString()} spawn gateway pid=${child.pid}\n`, "gateway.log");
  const pump = (chunk: Buffer) => appendLog(chunk.toString("utf8"), "gateway.log");
  child.stdout?.on("data", pump);
  child.stderr?.on("data", pump);
  return child;
}

// ── 健康检查 ──────────────────────────────────────────────

function healthOnce(port: number): Promise<boolean> {
  return new Promise((resolve) => {
    const req = http.get(
      { host: "127.0.0.1", port, path: "/health", timeout: 2000 },
      (res) => {
        res.resume();
        resolve((res.statusCode ?? 500) < 500);
      },
    );
    req.on("timeout", () => {
      req.destroy();
      resolve(false);
    });
    req.on("error", () => resolve(false));
  });
}

/**
 * 轮询 /health 直到就绪。后端首启要导入 opencv/onnxruntime 等重依赖，
 * 默认 90s 超时；onTick 每整秒回报一次已等待秒数（驱动 loading 页文案）。
 */
export async function waitForHealth(
  port: number,
  timeoutMs: number,
  onTick?: (elapsedS: number) => void,
): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  let lastReport = -1;
  while (Date.now() < deadline) {
    if (await healthOnce(port)) return;
    const elapsedS = Math.floor((timeoutMs - (deadline - Date.now())) / 1000);
    if (onTick && elapsedS !== lastReport) {
      lastReport = elapsedS;
      onTick(elapsedS);
    }
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error(
    `后端健康检查超时（${Math.round(timeoutMs / 1000)}s），日志：${join(logsDir(), "backend.log")}`,
  );
}

// ── 退出清理 ──────────────────────────────────────────────

/** 杀整个进程树（uvicorn → llama-server 等子进程一并带走；后端自身还有孤儿接管兜底） */
export async function killBackendTree(pid: number | undefined): Promise<void> {
  if (!pid) return;
  if (process.platform === "win32") {
    await new Promise<void>((resolve) => {
      const tk = spawn("taskkill", ["/pid", String(pid), "/T", "/F"], { windowsHide: true });
      tk.once("close", () => resolve());
      tk.once("error", () => resolve());
    });
  } else {
    try {
      process.kill(pid, "SIGKILL");
    } catch {
      /* 已退出 */
    }
  }
}
