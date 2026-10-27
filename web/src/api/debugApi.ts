// ── Debug 模块 API（可整体删除）：与正式接口零耦合 ──────────────
// 删除方式 = 删本文件 + components/DebugPanel.tsx + Chat 页挂载 +
// 后端 backend/api/routers/debug_api.py 及其 server.py 挂载行。
import axios from "axios";

const http = axios.create({ baseURL: "", timeout: 30000 });

export interface DebugMemoryAtom {
  record_id: string;
  content: string;
  kind: string;
  source_task: string;
  version: number;
  updated: string;
}

export interface DebugMemoryResponse {
  ok: boolean;
  project_id: string;
  project: { atoms: DebugMemoryAtom[] };
  global: { atoms: DebugMemoryAtom[] };
  counts: { project: number; global: number };
  error?: string;
}

export const debugApi = {
  // 记忆库只读转储（蒸馏结果）：项目库 + 全局库 atoms
  memory: (params: { task_id?: string; project_id?: string }) =>
    http.get<DebugMemoryResponse>("/api/debug/memory", { params }),
  // 删除单条记忆 atom（scope=project|global；物理删除，不可恢复，无二次确认）
  deleteMemory: (scope: "project" | "global", recordId: string, projectId?: string) =>
    http.post<{ ok: boolean; error?: string }>("/api/debug/memory/delete", {
      scope,
      record_id: recordId,
      project_id: projectId || "default",
    }),
};
