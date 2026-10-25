/**
 * 项目根（OmniAgent 仓库）解析——桌面壳定位后端代码与 .venv 的唯一入口。
 *
 * 优先级：
 * 1) 环境变量 OMNIAGENT_HOME 直接指向仓库
 * 2) omniagent.home 标记文件（内容为仓库绝对路径），查找位置：
 *    - 安装版：<安装目录>/resources/（electron-builder 的 process.resourcesPath）
 *    - 便携版：exe 同目录
 * 3) 开发态：desktop/ 就在仓库内（dist-electron → 上两级即仓库根）
 *
 * 命中标准：目标目录下同时存在 backend/server.py 与 .venv 的 python。
 */
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

// 打包进 main.mjs（ESM）后 import.meta.url 指向 dist-electron/，上两级即仓库根
const BUNDLE_DIR = dirname(fileURLToPath(import.meta.url));

let cached: string | null = null;

export function projectPython(root: string): string {
  const rel = process.platform === "win32"
    ? join(".venv", "Scripts", "python.exe")
    : join(".venv", "bin", "python");
  return join(root, rel);
}

function looksLikeRepo(dir: string): boolean {
  return existsSync(join(dir, "backend", "server.py")) && existsSync(projectPython(dir));
}

function readMarker(dir: string): string | null {
  const marker = join(dir, "omniagent.home");
  try {
    if (!existsSync(marker)) return null;
    return readFileSync(marker, "utf8").trim() || null;
  } catch {
    return null;
  }
}

let lastError = "未知原因";

export function resolveProjectRoot(): string {
  if (cached) return cached;
  const problems: string[] = [];

  const env = process.env.OMNIAGENT_HOME?.trim();
  if (env) {
    if (looksLikeRepo(env)) return (cached = env);
    problems.push(`OMNIAGENT_HOME=${env} 缺少 backend/server.py 或 .venv`);
  }

  const markerDirs = [
    process.resourcesPath,
    process.env.PORTABLE_EXECUTABLE_DIR, // electron-builder portable：原始 exe 所在目录
    dirname(process.execPath),
  ].filter(Boolean) as string[];
  for (const dir of markerDirs) {
    const target = readMarker(dir);
    if (!target) continue;
    if (looksLikeRepo(target)) return (cached = target);
    problems.push(`${join(dir, "omniagent.home")} → ${target} 缺少 backend/server.py 或 .venv`);
  }

  const devRoot = join(BUNDLE_DIR, "..", "..");
  if (looksLikeRepo(devRoot)) return (cached = devRoot);
  problems.push(`开发态目录 ${devRoot} 缺少 backend/server.py 或 .venv`);

  lastError = problems.join("\n");
  throw new Error(
    "无法定位 OmniAgent 仓库根。\n" + lastError +
    "\n解决方式：设置环境变量 OMNIAGENT_HOME 指向仓库，或在 exe 同目录放置 omniagent.home 文件（内容为仓库路径）。\n" +
    "注意：仓库内需已按 README 建好 .venv 虚拟环境。"
  );
}

export function projectRootError(): string {
  return lastError;
}

/** 不抛异常版本：config 读取等非关键路径用 */
export function resolveProjectRootSafe(): string | null {
  try {
    return resolveProjectRoot();
  } catch {
    return null;
  }
}
