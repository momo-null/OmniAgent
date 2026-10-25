/**
 * 最小 contextBridge：仅暴露只读信息，备将来 IPC 扩展。
 * UI 由后端同源承载，前端本身无需任何桌面特有 API。
 */
import { contextBridge } from "electron";

contextBridge.exposeInMainWorld("omniDesktop", {
  version: "1.0.0",
  platform: process.platform,
});
