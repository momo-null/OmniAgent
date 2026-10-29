/**
 * 桌面壳配置：读 config.yaml 的 desktop: 段，缺省用内置默认值。
 * 语义与后端 config.py 对齐：项目 config.yaml 为基底，~/.omniagent/config.yaml 深合并覆盖。
 */
import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import yaml from "js-yaml";

export interface DesktopConfig {
  /** 全局呼出/隐藏主窗口快捷键；置空串可禁用 */
  hotkey: string;
  /** 关闭窗口缩到托盘；false 时关窗即退出 */
  show_tray: boolean;
  /** 无边框窗口（原生三键覆盖层 + 顶部拖拽区）；false 用系统默认边框 */
  frameless: boolean;
  window: { width: number; height: number; min_width: number; min_height: number };
}

const DEFAULTS: DesktopConfig = {
  hotkey: "Ctrl+Shift+O",
  show_tray: true,
  frameless: true,
  window: { width: 1440, height: 900, min_width: 960, min_height: 600 },
};

function deepMerge<T>(base: T, override: unknown): T {
  if (override === null || override === undefined) return base;
  if (
    typeof base !== "object" || typeof override !== "object" ||
    Array.isArray(base) || Array.isArray(override)
  ) {
    return override as T;
  }
  const out: Record<string, unknown> = { ...(base as Record<string, unknown>) };
  for (const [k, v] of Object.entries(override as Record<string, unknown>)) {
    out[k] = k in out ? deepMerge((base as Record<string, unknown>)[k], v) : v;
  }
  return out as T;
}

function readYaml(path: string): Record<string, unknown> | null {
  try {
    if (!existsSync(path)) return null;
    const doc = yaml.load(readFileSync(path, "utf8"));
    return doc && typeof doc === "object" ? (doc as Record<string, unknown>) : null;
  } catch {
    return null; // 解析失败按缺失处理，用默认值
  }
}

export function loadDesktopConfig(projectRoot: string | null): DesktopConfig {
  let merged: Record<string, unknown> = {};
  const sources = [
    projectRoot ? join(projectRoot, "config.yaml") : null,
    join(homedir(), ".omniagent", "config.yaml"),
  ];
  for (const src of sources) {
    if (!src) continue;
    merged = deepMerge(merged, readYaml(src) ?? {});
  }
  const d = (merged.desktop ?? {}) as Record<string, unknown>;
  const w = (d.window ?? {}) as Record<string, unknown>;
  const num = (v: unknown, fallback: number) =>
    typeof v === "number" && Number.isFinite(v) && v > 0 ? v : fallback;
  return {
    hotkey: typeof d.hotkey === "string" ? d.hotkey : DEFAULTS.hotkey,
    show_tray: d.show_tray !== false,
    frameless: d.frameless !== false,
    window: {
      width: num(w.width, DEFAULTS.window.width),
      height: num(w.height, DEFAULTS.window.height),
      min_width: num(w.min_width, DEFAULTS.window.min_width),
      min_height: num(w.min_height, DEFAULTS.window.min_height),
    },
  };
}

/** 后端绑定地址（server.host，PC-0 绑定贯通）：与后端 server.py 同源读配置，缺省回环。 */
export function serverHost(projectRoot: string | null): string {
  let merged: Record<string, unknown> = {};
  const sources = [
    projectRoot ? join(projectRoot, "config.yaml") : null,
    join(homedir(), ".omniagent", "config.yaml"),
  ];
  for (const src of sources) {
    if (!src) continue;
    merged = deepMerge(merged, readYaml(src) ?? {});
  }
  const s = (merged.server ?? {}) as Record<string, unknown>;
  const host = typeof s.host === "string" ? s.host.trim() : "";
  return host || "127.0.0.1";
}
