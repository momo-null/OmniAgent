// ── Debug 模块 API（可整体删除）：与正式接口零耦合 ──────────────
// 删除方式 = 删本文件 + components/DebugPanel.tsx + Chat 页挂载 +
// 后端 backend/api/routers/debug_api.py 及其 server.py 挂载行。
import axios from "axios";

const http = axios.create({ baseURL: "", timeout: 30000 });

export interface DebugMemoryAtom {
  record_id: string;
  content: string;
  kind: string;
  persona?: string;
  origin?: string;
  source_task: string;
  importance?: number;
  salience?: number;
  access_count?: number;
  state?: string;
  updated: string;
}

export interface DebugMemoryOverview {
  total: number;
  by_state: Record<string, number>;
  by_kind: Record<string, number>;
  last_consolidation: string;
  recent: string[];
}

export interface DebugMemoryResponse {
  ok: boolean;
  atoms: DebugMemoryAtom[];
  count: number;
  overview?: DebugMemoryOverview;
  error?: string;
}

export const debugApi = {
  // 记忆库只读转储（brain.db 单库）
  memory: (params?: { task_id?: string }) =>
    http.get<DebugMemoryResponse>("/api/debug/memory", { params }),
  // 删除单条记忆（物理删除，不可恢复，无二次确认）
  deleteMemory: (recordId: string) =>
    http.post<{ ok: boolean; error?: string }>("/api/debug/memory/delete", {
      record_id: recordId,
    }),
};
