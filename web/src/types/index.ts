// 后端数据类型定义（与 backend 返回的 JSON 对齐）

export interface ModelInfo {
  name: string;
  description: string;
  gguf_path: string;
  size_mb: number;
  quant: string;
  gpu_layers: number;
  ctx_size: number;
  threads: number;
  port: number | null; // null = 自动分配
  reasoning_budget: number;
  tags: string[];
  mmproj_path: string | null;
  has_mmproj: boolean;
  has_meta: boolean; // 是否存在 .meta.json 侧注
  status: "running" | "stopped";
  pid: number | null;
}

export interface ActiveModel {
  name: string;
  port: number;
  pid: number | null;
  started_at: number | null;
  uptime_s: number;
  process_alive: boolean;
  healthy: boolean;
}

export interface ModelLogs {
  out: string;
  err: string;
}

export interface GpuInfo {
  name: string;
  memory_total_mb: number;
  memory_used_mb: number;
  memory_free_mb: number;
  error?: string;
}

// ── Plan C 前端实体（与 backend runtime_paths / task_store 对齐） ──
export interface Project {
  id: string; // 路径 slug
  path?: string;
  display_name?: string;
  last_used_at?: string;
}

export interface Task {
  task_id: string;
  project_id: string | null;
  objective: string;
  done_when?: string;
  state: "pending" | "running" | "done" | "failed" | "aborted";
  runs: string[];
  created_at: string;
  finished_at: string | null;
  success?: boolean;
}

export interface Message {
  role: "user" | "agent" | "system";
  text: string;
  ts: number;
  extra?: Record<string, unknown>;
}

export interface TrainingStatus {
  running: boolean;
  step: string;
  started_at: number | null;
  finished_at: number | null;
  last_error: string | null;
}

export type TrainingStep =
  | "dataset"
  | "train"
  | "merge"
  | "gguf"
  | "gguf-base"
  | "all";

// ── 模型精准控制（本地模型管理链路） ─────────────
export interface LaunchParams {
  threads?: number | null;
  ctx_size?: number | null;
  gpu_layers?: number | null;
  port?: number | null;
  reasoning_budget?: number | null;
  use_mmproj?: boolean | null;
  profile?: string | null;
  gguf_path?: string | null;
}

export interface ModelValidateRequest extends LaunchParams {
  prompt?: string;
  image_path?: string;
}

export interface ModelValidationResult {
  name: string;
  ok: boolean;
  reply: string;
  prompt: string;
  error: string | null;
  base_url?: string;
  model?: string;
}

// ── Plan C 运行时控制台（/api/runtime） ─────────────
export interface ProjectsResponse {
  projects: string[];
  tasks: string[];
}

export interface SkillInfo {
  name: string;
  objective_pattern: string;
  status: string;
  success_count: number;
  total_uses: number;
  substeps: { tool: string; args: Record<string, unknown> }[];
}

export interface WorldInfo {
  ok: boolean;
  summary?: string;
  collected_count?: number;
  checkpoints?: number;
}

export interface CollectedInfo {
  run_id?: string;
  items: unknown[];
  total: number;
  file?: string;
}

export interface TrajectoryStep {
  [key: string]: unknown;
}

export interface RuntimeStatus {
  running: boolean;
  task_id: string;
  project_id: string;
}

// agent 一轮的结构化步骤（思考 / 工具调用），与 ProcessItem 形状对齐
export interface ProcessStep {
  type: "thinking" | "message" | "tool_call";
  model?: string;
  content?: string;
  name?: string;
  arguments?: string;
  result?: string;
}

export interface ChatMsg {
  role: string;
  text: string;
  ts: number;
  // 结构化 agent turn：执行步骤 + 机器状态；前端据此把一轮渲染为「思考+工具+结论」一体
  extra?: {
    steps?: ProcessStep[];
    meta?: Record<string, unknown>;
    final?: boolean; // 是否为最终结论轮（前端以「最终结果」卡片独立呈现）
  };
}

// ── 对话区实时过程流（SSE `thinking` / `message` / `toolcall` 事件） ──
// 与右栏 debug 日志解耦：这里只承载「用户在对话框里想看到的」思考 + 口播 + 工具调用。
export interface ProcessItem {
  type: "thinking" | "message" | "tool_call";
  ts: number;
  id?: string; // 流式增量块标识：同一 id 的后续事件为 upsert（推理链/口播按轮累积）
  role?: string; // brain / executor（来源模型）
  model?: string;
  // thinking / message 块
  content?: string;
  // tool_call 卡片
  name?: string;
  arguments?: string;
  result?: string;
}

// ── tool 插件层（GET /api/runtime/tools） ─────────────
// 自研工具与外部 MCP 工具完全平级，用 source 区分来源
export interface ToolInfo {
  name: string;
  description: string;
  source: "core" | "env" | "plugin" | "mcp";
  unit: string; // 提供者标识：环境 kind / 插件名 / "core" / MCP server 名
  server: string | null; // 外部 MCP 工具所属 server；其余为 null
  parameters: Record<string, unknown>;
}

export interface EnvironmentInfo {
  kind: string;
  title: string;
  active: boolean;
}

export interface PluginInfo {
  name: string;
  title: string;
  description: string;
  enabled: boolean;
  requires_env: string[];
  available: boolean;
}

export interface McpServerInfo {
  name: string;
  enabled: boolean;
  command: string;
  args: string[];
  url: string;
  env_keys: string[];
}

export interface ToolsResponse {
  environments: EnvironmentInfo[];
  plugins: PluginInfo[];
  tools: ToolInfo[];
  mcp: {
    enabled: boolean;
    servers: McpServerInfo[];
    connected: string[]; // 已实际接入的 server 名
  };
}

export interface RuntimeSnapshot {
  skills: SkillInfo[];
  world: WorldInfo;
  collected: CollectedInfo;
  trajectory: TrajectoryStep[];
  running: boolean;
  task_id: string;
  project_id: string;
}

// ── K1 全局长期记忆（/api/runtime/memory） ─────────────
export interface MemoryIndex {
  master: string; // MEMORY.md 全文
  summary_chars: number; // 注入视图 memory_summary.md 字符数
  rollouts_total: number; // rollouts 文件总数
  merged_total: number; // 已合并进 MEMORY.md 的 task 数
  enabled: boolean; // 知识层弱注入（memory）是否开启
}

export interface RolloutInfo {
  task_id: string;
  distilled_at: string;
  success: boolean | null;
  facts_n: number;
  lessons_n: number;
  merged: boolean; // 是否已合并进 MEMORY.md
}

export interface RolloutsResponse {
  rollouts: RolloutInfo[];
  total: number;
}

export interface RolloutDetail {
  task_id: string;
  content: string; // rollout md 全文
  trajectory: string; // trajectory 引用路径
  facts_n: number;
  lessons_n: number;
  distilled_at: string;
  success: boolean | null;
}

// SSE debug 通道 memory_updated 事件载荷（Curator 蒸馏/合并后推送）
export interface MemoryUpdatedPayload {
  distilled: number;
  merged: number;
}

// ── K2 信号（/api/runtime/signals, /signals/summary） ─────────────
// 三实体一致率 + 聚合基线（设计 §5）
export interface SignalPoint {
  run_id: string;
  a: boolean; // 系统 success
  b: string; // 模型自报（assistant 结论，截断）
  c1: string; // 对话批准 approved/refuted/new_task/ambiguous
  c2: string; // 观测评审 success/fail/unknown
  mode: string;
  consistency: Record<string, number>;
  ts: string;
}

export interface SignalsSummary {
  fake_success_rate: {
    interactive: number | null;
    autonomous: number | null;
    overall: number | null;
    n_interactive: number;
    n_autonomous: number;
  } | null;
  miss_rate: unknown;
  divergence_rate: unknown;
  uncertain_rate: unknown;
  c1_dist: Record<string, number>;
  c2_dist: Record<string, number>;
  c1_calibration_error: number | null;
  total: number;
  updated_at: string;
  timeline: Array<{
    ts: string;
    c1: string;
    c2: string;
    a: boolean;
    mode: string;
  }>;
  note: string;
}

// ── K5 稳态（/api/runtime/signals/steady） ─────────────
export interface SteadyState {
  evaluated_at: string;
  domain: string;
  converged: boolean;
  thresholds: Record<string, unknown>;
  signals: {
    distill_dedup_hit_rate: number | null;
    skill_promotion_rate: { rate: number | null; active: number; total: number; series: number[] };
    step_variance: { variance: number | null; mean: number | null; n: number };
    human_intervention_rate: { rate: number | null; refuted: number; n_interactive: number };
    intervention_timeline: number[];
  };
  checks: Record<string, unknown>;
}

export interface SignalResponse {
  task_id: string;
  signals: SignalPoint[];
}
