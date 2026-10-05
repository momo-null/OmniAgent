// 后端数据类型定义（与 backend 返回的 JSON 对齐）

// ── 模型路由（~/.omniagent/models.json，/api/runtime/models） ──
// 目录与通用配置隔离：厂商/模型清单只在这里，config.yaml 保持引擎参数。
export interface ModelEntry {
  id: string; // 模型 id（请求体里的真实 model 字段）
  label: string; // 展示名（空则回退 id）
  vision: boolean;
}

export interface ModelProvider {
  id: string; // 目录内唯一 key（selection 形如 `${id}/${model.id}`）
  label: string; // 分组显示名
  base_url: string;
  api_key_set: boolean; // 明文不回传，仅表示已配置
  models: ModelEntry[];
}

// 设置页编辑用的原始条目（api_key 为空串 = 保持不变）
export interface ModelProviderDraft {
  label: string;
  base_url: string;
  api_key: string;
  api_key_env?: string;
  models: ModelEntry[];
}

export interface ModelSlotCurrent {
  selection: string; // 空 = 目录未选
  model: string; // 实际生效的模型 id
  base_url: string;
  provider_id: string;
  // catalog=目录真源；none=未配置
  source: "catalog" | "none";
}

export interface ModelsIndex {
  ok?: boolean;
  providers: ModelProvider[];
  defaults: Record<string, string>;
  current: Record<string, ModelSlotCurrent>;
  _meta?: { path: string; exists: boolean };
}

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
  extra_args: string[]; // 透传参数（llama.cpp argv token，随 .meta.json 侧注）
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
  extra_args?: string[] | null; // 透传参数（llama.cpp argv token）
  mmproj_path?: string | null;  // 显式投影文件；留空 = 自动探测同目录
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
  scope?: string; // project | global（知识分层 A6：合并目录标注来源）
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

// ── S2 审批卡（SSE `approval` 事件 / GET /live approvals 通道） ──
// 通用渲染（零工具特判）：风险标签由 risk 映射，参数整体以 JSON 展示
export interface ApprovalCardInfo {
  approval_id: string;
  task_id: string;
  tool: string;
  unit: string;
  risk: string; // exec | actuate | network | write
  arguments: Record<string, unknown>;
  created_at: number;
  wait_seconds: number; // 0 = 无限等
  ts?: number;
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

// ── P0 用户画像（/api/runtime/profile） ─────────────
export interface ProfileIndex {
  profile: string; // user_profile.md 全文（用户直接维护；LLM 自动维护待 A8 基建）
  enabled: boolean; // 画像注入是否开启（knowledge.profile.enabled，默认开）
}

// ── P0 角色卡（/api/runtime/character） ─────────────
export interface CharacterData {
  character: string; // character.md 全文（人格设定，随 system prompt 注入）
  name: string; // 从 frontmatter name: 解析的助手名，缺省 "OmniAgent"
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

