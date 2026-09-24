import axios from "axios";
import type {
  MemoryIndex,
  RolloutsResponse,
  RolloutDetail,
  SignalResponse,
  SignalsSummary,
  SteadyState,
} from "../types";

// 使用相对 baseURL，依赖 vite dev 代理将 /api 与 /v1 转发到后端 8000
const http = axios.create({
  baseURL: "",
  timeout: 30000,
  headers: { "Content-Type": "application/json" },
});

// ── 设置（GET/PUT /api/settings，白名单节） ───────────
export const settingsApi = {
  get: () => http.get("/api/settings"),
  put: (body: Record<string, unknown>) => http.put("/api/settings", body),
};

// ── 模型管理 ──────────────────────────────────────────
export const modelApi = {
  list: (models_dir?: string) =>
    http.get("/api/models", models_dir ? { params: { models_dir } } : {}),
  start: (name: string, body: Record<string, unknown> = {}) =>
    http.post(`/api/models/${name}/start`, body),
  stop: (name: string) => http.post(`/api/models/${name}/stop`),
  status: (name: string) => http.get(`/api/models/${name}/status`),
  logs: (name: string, lines = 100) =>
    http.get(`/api/models/${name}/logs`, { params: { lines } }),
  // 实时日志流（SSE）：返回 EventSource 的完整 URL
  logsStreamUrl: (name: string) => `/api/models/${name}/logs/stream`,
  validate: (name: string, body: Record<string, unknown> = {}) =>
    http.post(`/api/models/${name}/validate`, body),
  // 保存模型侧注（.meta.json）
  save: (name: string, body: Record<string, unknown>) =>
    http.post(`/api/models/${name}/save`, body),
  startPath: (body: Record<string, unknown>) =>
    http.post(`/api/models/start-path`, body),
  active: () => http.get("/api/active"),
  stopAll: () => http.post("/api/stop-all"),
  // C：强制释放所有 llama-server 进程（含未接管孤儿），后端崩溃残留也能清
  killOrphans: () => http.post("/api/models/kill-orphans"),
  scan: (models_dir?: string) =>
    http.get("/api/scan", models_dir ? { params: { models_dir } } : {}),
  gpu: () => http.get("/api/gpu"),
};

// ── Task 实体（独立平铺，方案 C） ──────────────────────
export const taskApi = {
  list: (state?: string) =>
    http.get("/api/runtime/tasks", state ? { params: { state } } : {}),
  get: (taskId: string) => http.get(`/api/runtime/tasks/${taskId}`),
  create: (body: { objective: string; done_when?: string; project_id?: string }) =>
    http.post("/api/runtime/tasks", body),
  updateState: (taskId: string, body: Record<string, unknown>) =>
    http.post(`/api/runtime/tasks/${taskId}/state`, body),
  rename: (taskId: string, objective: string) =>
    http.post(`/api/runtime/tasks/${taskId}/state`, { objective }),
  remove: (taskId: string) => http.delete(`/api/runtime/tasks/${taskId}`),
  skills: (taskId: string) => http.get(`/api/runtime/tasks/${taskId}/skills`),
  history: (taskId: string) => http.get(`/api/runtime/tasks/${taskId}/history`),
};

// ── Project（会话历史） ─────────────────────────────────
export const projectApi = {
  meta: () => http.get("/api/runtime/projects/meta"),
  sessions: (projectId: string) =>
    http.get(`/api/runtime/projects/${projectId}/sessions`),
  session: (projectId: string, sessionId: string, limit = 0) =>
    http.get(`/api/runtime/projects/${projectId}/sessions/${sessionId}`, { params: limit ? { limit } : {} }),
};

// ── Skill（独立 REST，按 task 维度） ───────────────────
export const skillApi = {
  run: (payload: { task_id?: string; skill_name: string }) =>
    http.post("/api/runtime/skill/run", payload),
  delete: (payload: { task_id?: string; skill_name: string }) =>
    http.post("/api/runtime/skill/delete", payload),
};

// ── OpenAI 兼容推理 ─────────────────────────────────────
export const llmApi = {
  chat: (payload: Record<string, unknown>) =>
    http.post("/v1/chat/completions", payload),
  complete: (payload: Record<string, unknown>) =>
    http.post("/v1/completions", payload),
  models: () => http.get("/v1/models"),
};

// ── 系统 ────────────────────────────────────────────────
export const systemApi = {
  health: () => http.get("/health"),
  gpu: () => http.get("/api/system/gpu"),
};

// ── K1 全局长期记忆（/api/runtime/memory） ─────────────
export const memoryApi = {
  // 只读聚合：master 全文 + summary 字符数 + rollouts/已合并计数 + 注入开关
  index: () => http.get<MemoryIndex>("/api/runtime/memory"),
  // 回放列表（倒序，支持分页）
  rollouts: (params?: { limit?: number; offset?: number }) =>
    http.get<RolloutsResponse>("/api/runtime/memory/rollouts", params ? { params } : {}),
  // 单条全文（含 trajectory 引用）
  rollout: (taskId: string) =>
    http.get<RolloutDetail>(`/api/runtime/memory/rollouts/${encodeURIComponent(taskId)}`),
  // 用户直写唯一入口：覆盖 MEMORY.md 并服务端重生成 summary
  update: (master: string) => http.put("/api/runtime/memory", { master }),
  // 重置：清空 memory/ 全部产物（需 ?confirm=reset 由后端校验）
  reset: () => http.delete("/api/runtime/memory?confirm=reset"),
  // 删除单条 rollout（不回滚已合并内容）
  deleteRollout: (taskId: string) =>
    http.delete(`/api/runtime/memory/rollouts/${encodeURIComponent(taskId)}`),
};

// ── K2/K5 信号（/api/runtime/signals*） ─────────────
export const signalsApi = {
  // 某任务各 run 三实体一致率 + 标签
  byTask: (taskId: string) =>
    http.get<SignalResponse>(`/api/runtime/signals`, { params: { task_id: taskId } }),
  // 聚合：假成功率（分模式）/ 漏报率 / 分歧率 / 不可判占比 / C₁·C₂ 分布 / 校准误差
  summary: () => http.get<SignalsSummary>("/api/runtime/signals/summary"),
  // K5 四信号 + 域收敛状态
  steady: () => http.get<SteadyState>("/api/runtime/signals/steady"),
};

// ── Plan C 运行时控制台（/api/runtime） ─────────────
export const runtimeApi = {
  projects: () => http.get("/api/runtime/projects"),
  snapshot: (taskId: string, projectId = "") =>
    http.get("/api/runtime/snapshot", { params: { task_id: taskId, project_id: projectId } }),
  // 当前轮实时过程快照：页面刷新 / 切任务后回放进行中的思考 + 工具调用
  live: (taskId: string) =>
    http.get("/api/runtime/live", { params: { task_id: taskId } }),
  skills: (taskId: string) =>
    http.get("/api/runtime/skills", { params: { task_id: taskId } }),
  // 统一 agent 入口：一个会话 = 一个 task；首条消息 task_id 留空由后端自动建
  chat: (payload: {
    messages: { role: string; content: string }[];
    task_id?: string;
    max_steps?: number;
    full_access?: boolean;
  }) => http.post("/api/runtime/chat", payload),
  // M8 软注入：运行中向 agent 插话（不打断当前 turn，下一轮可见）
  inject: (payload: { text: string; task_id?: string; agent_id?: string }) =>
    http.post("/api/runtime/inject", payload),
  // 用户主动停止（只有用户停止才停）
  stop: () => http.post("/api/runtime/stop"),
  skillRun: (payload: { task_id?: string; skill_name: string }) =>
    http.post("/api/runtime/skill/run", payload),
  skillDelete: (payload: { task_id?: string; skill_name: string }) =>
    http.post("/api/runtime/skill/delete", payload),
  // 工具插件层枚举（自研 + 外部 MCP 平级）
  tools: () => http.get("/api/runtime/tools"),
  // 环境单选（写 runtime.backend；重启后生效）
  setEnvironment: (kind: string) =>
    http.patch("/api/runtime/tools/environment", { kind }),
  // 插件开关（写 ~/.omniagent/plugins/<name>.yaml；重启后生效）
  setPluginEnabled: (name: string, enabled: boolean) =>
    http.patch("/api/runtime/tools/plugins", { name, enabled }),
  // SSE 流地址（EventSource 用，无需 axios）；支持 task_id 订阅特定 task 事件
  streamUrl: (taskId?: string) =>
    taskId ? `/api/runtime/stream?task_id=${encodeURIComponent(taskId)}` : "/api/runtime/stream",
};

export default http;
