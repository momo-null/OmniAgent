import axios from "axios";
import type {
  ProfileIndex,
  CharacterData,
  ModelsIndex,
  GitDiffResp,
  GitStatusResp,
  TerminalResp,
} from "../types";

// 使用相对 baseURL，依赖 vite dev 代理将 /api 与 /v1 转发到后端 8000
const http = axios.create({
  baseURL: "",
  timeout: 30000,
  headers: { "Content-Type": "application/json" },
});

// ── 鉴权（PC-0）：局域网/手机访问时后端要求 Bearer token ────────────
// 来源：页面 URL ?token=<server.auth_token>（首次粘贴带 token 的地址即记住）。
// 回环访问后端豁免，本机制只在非回环场景生效。
const _urlToken = new URLSearchParams(window.location.search).get("token");
if (_urlToken) localStorage.setItem("omni.auth_token", _urlToken);
export const authToken = localStorage.getItem("omni.auth_token") || "";
if (authToken) {
  http.defaults.headers.common["Authorization"] = `Bearer ${authToken}`;
}

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
  // 重命名 = 只改展示名（title）；objective 是模型侧任务目标，不随重命名改动
  rename: (taskId: string, title: string) =>
    http.post(`/api/runtime/tasks/${taskId}/state`, { title }),
  remove: (taskId: string) => http.delete(`/api/runtime/tasks/${taskId}`),
  openFolder: (taskId: string) =>
    http.post(`/api/runtime/tasks/${taskId}/open_folder`),
  skills: (taskId: string) => http.get(`/api/runtime/tasks/${taskId}/skills`),
  history: (taskId: string) => http.get(`/api/runtime/tasks/${taskId}/history`),
};

// ── Project（会话历史 + project 级知识资产；知识分层 C2/C3/C5） ──────────
export const projectApi = {
  meta: () => http.get("/api/runtime/projects/meta"),
  sessions: (projectId: string) =>
    http.get(`/api/runtime/projects/${projectId}/sessions`),
  session: (projectId: string, sessionId: string, limit = 0) =>
    http.get(`/api/runtime/projects/${projectId}/sessions/${sessionId}`, { params: limit ? { limit } : {} }),
  create: (body: { project_id: string; display_name?: string }) =>
    http.post("/api/runtime/projects", body),
  setAlias: (projectId: string, display_name: string) =>
    http.put(`/api/runtime/projects/${projectId}/alias`, { display_name }),
  remove: (projectId: string) =>
    http.delete(`/api/runtime/projects/${projectId}`),
};

// ── Skill（独立 REST，按 task 维度） ───────────────────
export const skillApi = {
  run: (payload: { task_id?: string; skill_name: string }) =>
    http.post("/api/runtime/skill/run", payload),
  delete: (payload: { task_id?: string; skill_name: string }) =>
    http.post("/api/runtime/skill/delete", payload),
  promote: (payload: { task_id?: string; skill_name: string }) =>
    http.post("/api/runtime/skill/promote", payload),
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

// ── P0 用户画像（/api/runtime/profile） ─────────────
export const profileApi = {
  // 画像正文 + 候选区 + 注入开关
  get: () => http.get<ProfileIndex>("/api/runtime/profile"),
  // 人工写画像正文（唯一人工入口，覆盖 user_profile.md）
  update: (profile: string) => http.put("/api/runtime/profile", { profile }),
};

// ── P0 角色卡（/api/runtime/character） ─────────────
export const characterApi = {
  // 角色卡全文 + 助手名
  get: () => http.get<CharacterData>("/api/runtime/character"),
  // 人工写角色卡（覆盖 character.md，下次运行生效）
  update: (character: string) => http.put("/api/runtime/character", { character }),
};

// ── S2 审批（/api/runtime/approvals + /audit） ─────────
export const approvalsApi = {
  // 人工决议一张待批卡（approve|deny，remember=本任务内不再询问同类操作）
  decide: (approvalId: string, action: "approve" | "deny", remember?: boolean) =>
    http.post(`/api/runtime/approvals/${encodeURIComponent(approvalId)}/decision`, {
      action,
      remember: !!remember,
    }),
  // 审计记录尾部 N 条（设置页「安全」区消费）
  audit: (limit = 200) =>
    http.get("/api/runtime/audit", { params: { limit } }),
};

// ── 模型路由（/api/runtime/models，目录存 ~/.omniagent/models.json） ──
export const modelCatalogApi = {
  // 分组目录 + 各槽位当前生效模型（api_key 不回传）
  list: () => http.get<ModelsIndex>("/api/runtime/models"),
  // 整体写回 providers（设置页维护；api_key 空串 = 保持不变）
  save: (providers: Record<string, unknown>) =>
    http.put("/api/runtime/models", { providers }),
  // 设置槽位默认模型；selection 为空串 = 跟随配置
  setDefault: (slot: string, selection: string) =>
    http.patch("/api/runtime/models/default", { slot, selection }),
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
    // 模型路由：本次请求使用的 "<provider_id>/<model_id>"；空 = 用目录默认/配置
    model?: string;
    // 立即发送（barge-in）：打断同一 task 的当前轮并立即发出；默认 false
    force?: boolean;
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
  // 插件开关（写 ~/.omniagent/plugins/<name>.yaml；重启后生效）
  setPluginEnabled: (name: string, enabled: boolean) =>
    http.patch("/api/runtime/tools/plugins", { name, enabled }),
  // SSE 流地址（EventSource 用，无需 axios）；支持 task_id 订阅特定 task 事件。
  // sub 非空 = 旁路订阅者（按游标读、不清空 outbox）——用于常驻收听跨进程的
  // tasks_changed（网关跑 QQ 等外部会话时，后端把「变了」推到全局 outbox）。
  streamUrl: (taskId?: string, sub?: string) => {
    const qs: string[] = [];
    if (taskId) qs.push(`task_id=${encodeURIComponent(taskId)}`);
    if (sub) qs.push(`sub=${encodeURIComponent(sub)}`);
    return qs.length ? `/api/runtime/stream?${qs.join("&")}` : "/api/runtime/stream";
  },
};

export default http;

// ── 右侧栏工作区（产物 / 文件预览 / git 变更 / 终端） ─────────────
export const workspaceApi = {
  // 任务产物登记表（右侧栏「文件」页与结果卡「产物」同源）
  artifacts: (taskId: string) =>
    http.get("/api/runtime/artifacts", { params: { task_id: taskId } }),
  // 产物文本预览（图片/二进制只回元信息，内容走 fileRawUrl）
  readFile: (taskId: string, path: string) =>
    http.get("/api/runtime/files/read", { params: { task_id: taskId, path }, timeout: 20000 }),
  // 原文件流（<img> 预览 / 下载）；download=1 时以附件下发
  fileRawUrl: (taskId: string, path: string, download?: boolean) =>
    `/api/runtime/files/raw?task_id=${encodeURIComponent(taskId)}&path=${encodeURIComponent(path)}${download ? "&download=1" : ""}`,
  gitStatus: (path: string) =>
    http.get<GitStatusResp>("/api/runtime/git/status", { params: { path }, timeout: 30000 }),
  gitDiff: (path: string, file: string) =>
    http.get<GitDiffResp>("/api/runtime/git/diff", { params: { path, file }, timeout: 30000 }),
  terminal: (body: { command: string; cwd?: string; shell?: string; timeout_sec?: number }) =>
    http.post<TerminalResp>("/api/runtime/terminal", body, { timeout: 320000 }),
};

// ── 定时任务（GET/PUT /api/cron/jobs；jobs.json 热加载） ───────────
export const cronApi = {
  list: () => http.get("/api/cron/jobs"),
  save: (jobs: Record<string, unknown>[]) => http.put("/api/cron/jobs", { jobs }),
  run: (id: string) => http.post(`/api/cron/jobs/${encodeURIComponent(id)}/run`),
  projects: () => http.get("/api/cron/projects"),
  runs: (jobId = "", limit = 100) =>
    http.get(`/api/cron/runs?job_id=${encodeURIComponent(jobId)}&limit=${limit}`),
};

// ── 语音（/api/audio/*；voice 插件未启用时后端 404） ──────────────
export const audioApi = {
  // 录音 WAV → 文本（微信对讲式按住说话）
  transcribe: async (blob: Blob): Promise<string> => {
    const res = await fetch("/api/audio/transcribe", {
      method: "POST",
      headers: authToken ? { "Content-Type": "audio/wav", Authorization: `Bearer ${authToken}` } : { "Content-Type": "audio/wav" },
      body: blob,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) throw new Error(data.error || `转写失败 (${res.status})`);
    return String(data.text || "");
  },
  // 文本 → WAV 音频（回复播报）
  tts: async (text: string): Promise<Blob> => {
    const res = await fetch("/api/audio/tts", {
      method: "POST",
      headers: authToken ? { "Content-Type": "application/json", Authorization: `Bearer ${authToken}` } : { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (!res.ok) {
      let msg = `合成失败 (${res.status})`;
      try {
        const d = await res.json();
        if (d?.error) msg = d.error;
      } catch {
        // 非 JSON 错误体，用默认消息
      }
      throw new Error(msg);
    }
    return await res.blob();
  },
};
