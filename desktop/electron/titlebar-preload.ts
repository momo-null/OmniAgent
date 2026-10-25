/**
 * 导航条专用 preload：仅暴露导航按钮的 IPC（titlebar.html 使用）。
 * 与内容视图的 preload.cjs 分开——内容页（web UI）拿不到这些能力。
 */
import { contextBridge, ipcRenderer } from "electron";

contextBridge.exposeInMainWorld("omniNav", {
  toggleSidebar: (): Promise<boolean> => ipcRenderer.invoke("nav:toggle-sidebar"),
  newTask: (): Promise<boolean> => ipcRenderer.invoke("nav:new-task"),
});
