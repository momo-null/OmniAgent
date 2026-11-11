// ── Debug 模块 API（可整体删除）：与正式接口零耦合 ──────────────
// 删除方式 = 删本文件 + components/DebugPanel.tsx + components/debug/ +
// 右侧栏挂载 + 后端 backend/api/routers/debug_api.py、debug_memory_data.py 与 server.py 挂载行。
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
  active: number;
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

export interface MemoryDocument {
  layer: string;
  kind: string;
  name: string;
  key: string;
  chars: number;
  bytes?: number;
  updated: string;
}

export interface MemoryLandscape {
  ok: boolean;
  error?: string;
  generated?: string;
  enabled?: boolean;
  brain?: {
    path?: string; bytes?: number; total?: number; active?: number;
    by_kind?: Record<string, number>; by_state?: Record<string, number>;
    by_narrative_kind?: Record<string, number>;
    atoms_fts?: number; l0?: number; l0_fts?: number;
    narrative?: number; narrative_fts?: number;
    circle_notes?: number; context_buffers?: number;
    tables?: string[]; meta?: Record<string, string>;
  };
  health?: {
    enabled?: boolean; atoms_fts_gap?: number; l0_fts_gap?: number;
    narrative_fts_gap?: number; last_consolidation?: string; flush_count?: string;
  };
  l0?: { count?: number; session_files?: number; recent?: Record<string, unknown>[] };
  l2?: {
    narrative_index?: number; by_kind?: Record<string, number>;
    scenes?: Record<string, unknown>[]; documents?: Record<string, number>;
  };
  l3?: Record<string, { exists: boolean; chars: number; updated: string; preview: string }>;
  context?: {
    count?: number; notes?: Record<string, unknown>[]; buffers?: Record<string, unknown>[];
  };
  documents?: MemoryDocument[];
  writer?: Record<string, unknown>;
}

export interface MemoryAtomRow {
  record_id: string;
  content: string;
  kind?: string;
  state?: string;
  salience?: number;
  importance?: number;
  access_count?: number;
  origin?: string;
  persona?: string;
  source_task?: string;
  superseded_by?: string;
  version?: number;
  created?: string;
  updated?: string;
  last_access?: string;
  context_key?: string;
  source_kind?: string;
  [key: string]: unknown;
}

export interface PageResponse<T> {
  ok: boolean;
  error?: string;
  total: number;
  count: number;
  limit?: number;
  offset?: number;
  items: T[];
}

export interface NarrativeRow {
  doc_id: string;
  kind: string;
  project_id?: string;
  date_from?: string;
  date_to?: string;
  summary?: string;
  topics?: string;
  context_keys?: string;
  peers?: string;
  atmosphere?: string;
  date_basis?: string;
  updated?: string;
  [key: string]: unknown;
}

export interface DocumentText {
  ok: boolean;
  error?: string;
  text?: string;
  chars?: number;
  truncated?: boolean;
  key?: string;
  name?: string;
}

export interface MetricsReport {
  ok: boolean;
  error?: string;
  generated?: string;
  observation?: {
    runs?: number; calls?: number; hits?: number;
    hit_rate?: number | null; zero_runs?: number; zero_rate?: number | null;
    avg_context_chars?: number | null; context_injected_runs?: number;
    narrative_cross?: number; external_runs?: number; llm_calls_delta?: number;
    evidence_runs?: number; bundle_taken?: number; quota_blocked?: number;
    no_candidate?: number; missed_candidate?: number; fetched?: number;
    missed_rate?: number | null;
    by_tool?: Record<string, number>;
    daily?: { date: string; runs: number; calls: number; hits: number; zero: number;
              context_chars: number; missed?: number }[];
    last_ts?: string; files?: number; note?: string;
  };
  effect_report?: {
    available: boolean; generated?: string; note?: string; error?: string;
    l1?: Record<string, unknown>; background?: Record<string, unknown>;
    narrative?: Record<string, unknown>; narrative_self_fetch?: Record<string, unknown>;
    p0_regression?: { now?: Record<string, number>; verdict?: Record<string, boolean>; pass?: boolean | null };
    silent_cost?: Record<string, unknown>; pending_human?: Record<string, unknown>;
  };
  telemetry?: {
    runs?: number;
    averages?: Record<string, number>;
    recent?: { run_id?: string; success?: boolean; steps?: number; metrics?: Record<string, number> }[];
  };
  writer?: Record<string, unknown>;
  store?: {
    active_atoms?: number; inactive_atoms?: number; avg_salience?: number;
    max_salience?: number; total_access?: number; max_access?: number;
    last_consolidation?: string; flush_count?: string;
  };
}

export const debugApi = {
  // 旧版兼容转储（单库 atoms + overview）
  memory: (params?: { task_id?: string; limit?: number }) =>
    http.get<DebugMemoryResponse>("/api/debug/memory", { params }),
  // 手动注销关系卡（admin：删正文+撤索引；主人卡拒绝）
  deleteRelationCard: (uid: string) =>
    http.delete(`/api/debug/memory/relation-card`, { params: { uid } }),
  deleteMemory: (recordId: string) =>
    http.post<{ ok: boolean; error?: string }>("/api/debug/memory/delete", {
      record_id: recordId,
    }),

  // 记忆全景（只读）
  landscape: () => http.get<MemoryLandscape>("/api/debug/memory/landscape"),
  atoms: (params: { limit?: number; offset?: number; state?: string; kind?: string; q?: string }) =>
    http.get<PageResponse<MemoryAtomRow>>("/api/debug/memory/atoms", { params }),
  l0: (params: { limit?: number; offset?: number; q?: string; context_key?: string; source_kind?: string }) =>
    http.get<PageResponse<Record<string, unknown>>>("/api/debug/memory/l0", { params }),
  narrative: (params: { limit?: number; kind?: string; q?: string }) =>
    http.get<PageResponse<NarrativeRow>>("/api/debug/memory/narrative", { params }),
  documents: () =>
    http.get<{ ok: boolean; items: MemoryDocument[]; error?: string }>("/api/debug/memory/documents"),
  readDocument: (key: string) =>
    http.get<DocumentText>("/api/debug/memory/document", { params: { key } }),

  // 监控指标（只读）
  metrics: () => http.get<MetricsReport>("/api/debug/memory/metrics"),

  // 主人控制面（纠正 = tombstone 旧条 + 新条；遗忘 = 软删）
  correctAtom: (recordId: string, newText: string) =>
    http.post<{ ok: boolean; new_record_id?: string; error?: string }>(
      "/api/debug/memory/correct", { record_id: recordId, new_text: newText }),
  forgetAtom: (recordId: string, reason = "owner-forget") =>
    http.post<{ ok: boolean; error?: string }>(
      "/api/debug/memory/forget", { record_id: recordId, reason }),
};
