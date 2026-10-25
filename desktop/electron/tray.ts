/**
 * 系统托盘（吸收 OmniAgent-v3 经验并补齐）：
 * - 关闭窗口 = 隐藏到托盘（主进程 close 事件处理）
 * - 真正退出前置 tray=null 再 app.quit()，否则 close handler 的 preventDefault
 *   会把退出拦死（v3 踩坑点）
 * - 图标优先 resources/icon.png（scripts/make_icon.py 生成），
 *   缺失时回退代码画的像素圆（v3 做法，不依赖任何资源文件）
 */
import { Menu, nativeImage, Tray, type NativeImage } from "electron";
import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

// 与 main.ts 同理：打包后指向 dist-electron/，resources 在上一级
const BUNDLE_DIR = dirname(fileURLToPath(import.meta.url));

let tray: Tray | null = null;

function pixelIcon(): NativeImage {
  // 16x16 青色圆环，边缘 1px 抗锯齿（v3 同款兜底图标）
  const size = 16;
  const buf = Buffer.alloc(size * size * 4);
  const c = (size - 1) / 2;
  const r = size * 0.38;
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const inner = Math.abs(Math.hypot(x - c, y - c) - r);
      const a = inner <= 1 ? Math.round(255 * (1 - inner)) : 0;
      const i = (y * size + x) * 4;
      buf[i] = 46;
      buf[i + 1] = 208;
      buf[i + 2] = 255;
      buf[i + 3] = a;
    }
  }
  return nativeImage.createFromBuffer(buf, { width: size, height: size, scaleFactor: 1.0 });
}

function trayIcon(): NativeImage {
  const png = join(BUNDLE_DIR, "..", "resources", "icon.png");
  if (existsSync(png)) {
    const img = nativeImage.createFromPath(png).resize({ width: 16, height: 16 });
    if (!img.isEmpty()) return img;
  }
  return pixelIcon();
}

export interface TrayActions {
  showWindow(): void;
  restartBackend(): void;
  openLogs(): void;
  quit(): void;
}

export function createTray(actions: TrayActions): void {
  try {
    tray = new Tray(trayIcon());
  } catch {
    return; // 托盘创建失败不阻断主流程
  }
  tray.setToolTip("OmniAgent 启动中…");
  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: "显示主界面", click: () => actions.showWindow() },
      { label: "重启后端", click: () => actions.restartBackend() },
      { label: "打开日志文件夹", click: () => actions.openLogs() },
      { type: "separator" },
      { label: "退出 OmniAgent", click: () => actions.quit() },
    ]),
  );
  tray.on("click", () => actions.showWindow());
}

export function setTrayStatus(text: string): void {
  tray?.setToolTip(`OmniAgent — ${text}`);
}

/** 真正退出前调用：置空 tray 引用，绕过 close→hide 拦截（v3 经验） */
export function disposeTray(): void {
  tray = null;
}
