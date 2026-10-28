// 全局 UI 状态：当前视图、task 列表、当前 task、消息流、SSE 连接
// 单页 shell，视图通过 state 切换（无路由跳转）
// 使用 React Context 实现，不引入额外依赖。
import React, { createContext, useContext, useMemo, useRef, useState, useCallback, useEffect } from "react";
import { runtimeApi, taskApi, projectApi, settingsApi, approvalsApi } from "../api/client";
import type { ChatMsg, RuntimeSnapshot, SkillInfo, ProcessItem, ApprovalCardInfo } from "../types";

export interface DebugLog {
  ts: number;
  kind: string;
  role?: string;
  title: string;
  payload: Record<string, unknown>;
}

export type MainView = "chat" | "skills" | "cron" | "settings";

// 右侧栏（RightDock）页签：文件/产物 · 浏览器 · git 变更 · 终端 · Debug
export type DockTab = "files" | "browser" | "git" | "terminal" | "debug";
// dock 窗口内页签视图：home=主页（入口列表），其余为功能视图
export type DockView = "home" | DockTab;
// 浏览器式页签（每页签记住自己停在哪个视图）
export interface DockTabItem { id: string; view: DockView; }

interface TaskStoreValue {
  view: MainView;
  setView: (v: MainView) => void;

  tasks: string[];
  projects: string[];
  // 知识分层 C4：task → project_id 映射（侧栏显示项目名 / 保存到项目 / 删项目计数）
  taskProjects: Record<string, string>;
  currentTaskId: string;
  setCurrentTaskId: (id: string) => void;
  selectTask: (id: string) => void;
  refreshTasks: () => Promise<void>;
  deleteTask: (taskId: string) => Promise<void>;
  renameTask: (taskId: string, title: string) => Promise<void>;

  running: boolean;

  messages: ChatMsg[];
  pushMessage: (m: ChatMsg) => void;
  clearMessages: () => void;

  snapshot: RuntimeSnapshot;
  setSnapshot: (s: RuntimeSnapshot) => void;

  connectStream: () => void;
  disconnectStream: () => void;
  // 模型路由：model 为本次请求的选择 "<provider_id>/<model_id>"；空 = 用目录默认/配置
  sendMessage: (text: string, fullAccess?: boolean, model?: string) => Promise<void>;
  // 立即发送（barge-in）：打断同一 task 的当前轮并立即发出（后端 /chat force=true）
  forceSendMessage: (text: string, fullAccess?: boolean, model?: string) => Promise<void>;
  // 排队条目：仅当注入被拒（收尾窗口/无运行体）时兜底入队；轮次结束自动作为新消息发出
  queuedMessages: string[];
  removeQueuedAt: (index: number) => void;
  // 运行中发送入口：先注入本轮（M8）；注入被拒才排队，两路互斥绝不双重投递
  injectOrQueue: (text: string) => void;
  // M8 软注入：运行中向 agent 插话（不打断当前步骤，下一请求边界可见）；返回是否成功
  injectMessage: (text: string) => Promise<boolean>;
  stopTask: () => void;

  maxSteps: number;
  setMaxSteps: (n: number) => void;

  // 完全访问按 task 持久化：每个 task 各自写自己的 task.json，跟随当前任务，切换任务时读取各自值
  fullAccessByTask: Record<string, boolean>;
  setFullAccessForTask: (taskId: string, val: boolean) => void;

  taskTitles: Record<string, string>;
  taskObjectives: Record<string, string>;

  debugLogs: DebugLog[];
  pushDebugLog: (m: DebugLog) => void;
  clearDebugLogs: () => void;

  processLogs: ProcessItem[];
  pushProcessLog: (m: ProcessItem) => void;
  clearProcessLogs: () => void;

  // S2 审批：待批卡（顶置在 Chat 输入框上方）+ 决议入口
  pendingApprovals: ApprovalCardInfo[];
  decideApproval: (approvalId: string, action: "approve" | "deny", remember?: boolean) => Promise<void>;

  // 右侧 dock 窗口：整体收起（宽度 0）/展开为固定 440px 窗口；浏览器式页签管理，
  // 主页=入口列表（窗口居中），点入口当前页签跳转，+ 新建页签（默认主页）
  dockOpen: boolean;
  dockTabs: DockTabItem[];
  dockActiveId: string;
  dockPreviewPath: string; // files 页待预览文件（空 = 未选）
  openDock: (tab: DockTab, previewPath?: string) => void; // 打开/聚焦某功能视图（产物卡等入口）
  openDockMenu: () => void; // 打开窗口并停在主页
  closeDock: () => void;
  newDockTab: () => void; // + 新建页签（主页）
  closeDockTab: (id: string) => void;
  activateDockTab: (id: string) => void;
  navigateDockTab: (view: DockTab) => void; // 当前页签跳到某功能视图
  setDockPreviewPath: (path: string) => void;
  // 文件页产物清单版本号：结果卡点击「查看所有产物」等入口触发文件页刷新
  artifactsVersion: number;
  bumpArtifactsVersion: () => void;

}

// dock 页签 id 自增序号（模块级，刷新页面重置无妨）
let dockTabSeq = 0;

const EMPTY_SNAPSHOT: RuntimeSnapshot = {
  skills: [],
  world: { ok: false },
  collected: { items: [], total: 0 },
  trajectory: [],
  running: false,
  task_id: "",
  project_id: "",
};

const TaskStoreContext = createContext<TaskStoreValue | null>(null);

export function TaskStoreProvider({ children }: { children: React.ReactNode }) {
  const [view, setView] = useState<MainView>("chat");
  const [tasks, setTasks] = useState<string[]>([]);
  const [projects, setProjects] = useState<string[]>([]);
  const [currentTaskId, setCurrentTaskId] = useState("");
  const [running, setRunning] = useState(false);
  // 排队消息（软注入的替代形态）：运行中发送 → 入队不打断；轮末自动接力，或点「立即」强制发出
  const [queuedMessages, setQueuedMessages] = useState<string[]>([]);
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [snapshot, setSnapshot] = useState<RuntimeSnapshot>(EMPTY_SNAPSHOT);
  const [maxSteps, setMaxSteps] = useState<number>(40);
  const [fullAccessByTask, setFullAccessByTask] = useState<Record<string, boolean>>({});
  const [taskTitles, setTaskTitles] = useState<Record<string, string>>(() => {
    try {
      const raw = localStorage.getItem("omniagent.taskTitles");
      return raw ? (JSON.parse(raw) as Record<string, string>) : {};
    } catch {
      return {};
    }
  });
  // 服务端主标题源：后端 task.objective（P2.2：标题以服务端为主，localStorage 仅作 rename 覆盖）
  const [taskObjectives, setTaskObjectives] = useState<Record<string, string>>({});
  const [taskProjects, setTaskProjects] = useState<Record<string, string>>({});
  const [debugLogs, setDebugLogs] = useState<DebugLog[]>([]);
  // 对话区实时过程流（思考 + 工具调用），与右栏 debug 日志解耦
  const [processLogs, setProcessLogs] = useState<ProcessItem[]>([]);
  // S2 审批：待批卡（SSE approval 事件 / live approvals 通道，按 approval_id upsert）
  const [pendingApprovals, setPendingApprovals] = useState<ApprovalCardInfo[]>([]);

  // 右侧 dock 窗口状态：默认整体收起；页签存于单个 state（tabs+activeId）便于原子更新
  const [dockOpen, setDockOpen] = useState(false);
  const [dockWin, setDockWin] = useState<{ tabs: DockTabItem[]; activeId: string }>({ tabs: [], activeId: "" });
  const [dockPreviewPath, setDockPreviewPath] = useState("");
  const [artifactsVersion, setArtifactsVersion] = useState(0);

  // 标题写入时同步持久化到 localStorage（纯前端，不进后端内核）
  const persistTitles = useCallback((next: Record<string, string>) => {
    try {
      localStorage.setItem("omniagent.taskTitles", JSON.stringify(next));
    } catch {
      /* 忽略存储异常 */
    }
  }, []);

  const esRef = useRef<EventSource | null>(null);
  const runningRef = useRef(false);
  runningRef.current = running;
  // 排队队列：ref 是唯一权威（接力/入队同步改 ref 再镜像 state，防 SSE 连帧双发）
  const queuedRef = useRef<string[]>([]);
  const sendMessageRef = useRef<((text: string, fullAccess?: boolean, model?: string) => Promise<void>) | null>(null);
  const prevRunningRef = useRef(false);  // status false→true = 新 run 启动（清过程流时机）
  const pumpTimerRef = useRef<number | null>(null);  // 接力防抖：一轮只发一条，等 status 确认
  const messagesRef = useRef<ChatMsg[]>([]);
  messagesRef.current = messages;
  const currentTaskIdRef = useRef<string>("");
  currentTaskIdRef.current = currentTaskId;

  const pushMessage = useCallback((m: ChatMsg) => {
    setMessages((prev) => [...prev.slice(-300), m]);
  }, []);

  const clearMessages = useCallback(() => setMessages([]), []);

  const pushDebugLog = useCallback((m: DebugLog) => {
    setDebugLogs((prev) => [...prev.slice(-500), m]);
  }, []);
  const clearDebugLogs = useCallback(() => setDebugLogs([]), []);

  const pushProcessLog = useCallback((m: ProcessItem) => {
    setProcessLogs((prev) => [...prev.slice(-300), m]);
  }, []);
  const clearProcessLogs = useCallback(() => setProcessLogs([]), []);

  // 流式增量：带 id 的过程项按 id upsert（同一块内容原地更新，避免重复刷屏）
  const upsertProcessLog = useCallback((m: ProcessItem) => {
    if (m.id) {
      setProcessLogs((prev) => {
        const idx = prev.findIndex((p) => (p as ProcessItem).id === m.id);
        if (idx >= 0) {
          const next = prev.slice();
          next[idx] = { ...prev[idx], ...m };
          return next;
        }
        return [...prev.slice(-300), m];
      });
    } else {
      pushProcessLog(m);
    }
  }, [pushProcessLog]);

  // P2.2: 切换 task 时加载历史会话
    const loadHistory = useCallback(async (taskId: string) => {
    if (!taskId) {
      clearMessages();
      clearProcessLogs();
      return;
    }
    try {
      const r = await taskApi.history(taskId);
      const d = r.data as {
        messages: { role: string; content: string; ts?: string; extra?: ChatMsg["extra"] }[];
      };
      const msgs: ChatMsg[] = (d.messages || []).map((m, i) => ({
        role: m.role,
        text: m.content,
        ts: m.ts ? Date.parse(m.ts) : Date.now() + i,
        extra: m.extra,
      }));
      // 整体替换（非合并）：每个 task 的对话严格独立，避免切会话时把上一个会话
      // 的残留消息 merge 进当前会话导致「跨会话串台」。后端在 /chat POST 时已把用户
      // 消息落库（ProjectStore.append_message("user", ...)），故此处替换不会丢消息；
      // 发送瞬间的乐观渲染（pushMessage）在 status 触发回拉前已落在服务端历史里。
      setMessages(msgs.slice(-300));
    } catch {
      clearMessages();
    }
  }, [clearMessages]);

  // 回放「进行中这一轮」的实时过程：该轮只存在于后端内存（outbox 被 SSE 消费即清空，
  // 且轮末才落盘），刷新/切任务后前端 processLogs 为空、会话历史里也没有它 → 表现为
  // 「执行中全程无输出，直到本轮结束才一次性出现」。这里在挂载/切任务时拉一次
  // /live 快照种进去；后端 running=false 时不种（该轮已结束，历史里已有完整轮次）。
  const seedLiveLogs = useCallback(async (taskId: string) => {
    if (!taskId) return;
    try {
      const r = await runtimeApi.live(taskId);
      const d = r.data as {
        running?: boolean;
        thinking?: ProcessItem[];
        message?: ProcessItem[];
        toolcall?: ProcessItem[];
        approvals?: ApprovalCardInfo[];
      };
      // S2：进行中的待批卡一并恢复（刷新 / 切任务后重建顶置卡，§6.3）
      setPendingApprovals(d.approvals || []);
      if (!d?.running) return;
      const items: ProcessItem[] = [
        ...(d.thinking || []).map((x) => ({ ...x, type: "thinking" as const })),
        ...(d.message || []).map((x) => ({ ...x, type: "message" as const })),
        ...(d.toolcall || []).map((x) => ({ ...x, type: "tool_call" as const })),
      ]
        // 后端 outbox 的 ts 是秒级 time.time()，统一转毫秒与 SSE 增量对齐
        .map((x) => ({ ...x, ts: Number(x.ts) * 1000 }))
        .sort((a, b) => a.ts - b.ts);
      if (!items.length) return;
      setProcessLogs((prev) => {
        // 按 id 合并：快照为准；prev 里 id 不在快照内的（快照之后才到的增量）保留
        const ids = new Set(items.map((x) => x.id));
        const keep = prev.filter((p) => p.id !== undefined && !ids.has(p.id));
        return [...items, ...keep].sort((a, b) => a.ts - b.ts).slice(-300);
      });
    } catch {
      /* 忽略瞬时错误 */
    }
  }, []);

    const selectTask = useCallback((taskId: string) => {
    setCurrentTaskId(taskId);
    try { localStorage.setItem("omniagent.currentTaskId", taskId); } catch { /* ignore */ }
    clearProcessLogs();
    setPendingApprovals([]);
    queuedRef.current = []; // 排队消息不跨会话携带（ref 权威 + state 镜像）
    setQueuedMessages([]);
    if (pumpTimerRef.current != null) {
      clearTimeout(pumpTimerRef.current);
      pumpTimerRef.current = null;
    }
    prevRunningRef.current = false;
    loadHistory(taskId);
    // 回放进行中这一轮的实时过程（见 seedLiveLogs 注释）
    seedLiveLogs(taskId);
  }, [loadHistory, clearProcessLogs, seedLiveLogs]);

  // 刷新后恢复上次打开的 task（其 agent 消息含 steps，toolcall/思考随之重建，避免「刷新即清零」）
  useEffect(() => {
    try {
      const saved = localStorage.getItem("omniagent.currentTaskId");
      if (saved) selectTask(saved);
    } catch { /* ignore */ }
    // 仅在挂载时执行一次
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    // 初始化默认步数上限：从后端 config（runtime.default_max_steps）读取，
    // 使会话默认预算与 Web 设置面板一致（写入 ~/.omniagent/config.yaml 的值）。
    useEffect(() => {
      settingsApi.get().then((r) => {
        const v = (r.data as any)?.runtime?.default_max_steps;
        if (typeof v === "number" && v > 0) setMaxSteps(v);
      }).catch(() => {});
      // 仅在挂载时执行一次
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

  // 任意时刻当前任务变化都持久化（含 SSE 推送新 tid 时 setCurrentTaskId 的路径，
  // 该路径不经过 selectTask，原先不会写 localStorage → 刷新后加载到旧/空 id 而丢失全部记录）
  useEffect(() => {
    if (!currentTaskId) return; // 空值（新建对话）不覆盖已保存项
    try { localStorage.setItem("omniagent.currentTaskId", currentTaskId); } catch { /* ignore */ }
  }, [currentTaskId]);

  const refreshTasks = useCallback(async () => {
    try {
      const r = await taskApi.list();
      const d = r.data as { tasks: { task_id: string; objective?: string }[] };
      setTasks((d.tasks || []).map((t) => t.task_id));
      // P2.2: 服务端 objective 作为标题主源（用户 rename 后用 taskTitles 覆盖）
      setTaskObjectives((prev) => {
        const next = { ...prev };
        for (const t of d.tasks || []) {
          if (t.objective) next[t.task_id] = t.objective;
        }
        return next;
      });
      // C4：task → project 归属（侧栏项目名展示 / 删除项目时计数）
      setTaskProjects((prev) => {
        const next = { ...prev };
        for (const t of d.tasks || []) {
          const pid = (t as { project_id?: string }).project_id;
          if (pid) next[t.task_id] = pid;
        }
        return next;
      });
      // 按 task 从服务端 task.json 回填完全访问默认值（跟随当前任务，切换任务读取各自值）
      setFullAccessByTask((prev) => {
        const next = { ...prev };
        for (const t of d.tasks || []) {
          next[t.task_id] = !!(t as { full_access?: boolean }).full_access;
        }
        return next;
      });
    } catch {
      /* 忽略瞬时错误 */
    }
    try {
      const pm = await projectApi.meta();
      const pd = pm.data as { projects: { id: string }[] };
      setProjects((pd.projects || []).map((p) => p.id));
    } catch {
      /* 忽略瞬时错误 */
    }
  }, []);

  const connectStream = useCallback(() => {
    // 重连前先断开旧连接，保证订阅的 task_id 始终与当前会话一致
    // （首条消息拿到 tid / 新建对话切到 "" 都靠 currentTaskId 变化触发重连）
    if (esRef.current) {
      esRef.current.close();
      esRef.current = null;
    }
    // P1.5: SSE 订阅当前 task（如有）；后端按 task_id 隔离事件，不同 task 不串消息
    const tid = currentTaskIdRef.current;
    const es = new EventSource(runtimeApi.streamUrl(tid || undefined));
    esRef.current = es;

    es.addEventListener("chat", (ev) => {
      try {
        const m = JSON.parse((ev as MessageEvent).data) as ChatMsg;
        // B1：后端 outbox ts 为秒级 time.time()，统一转毫秒——与
        // thinking/toolcall/message listener 及 loadHistory(Date.parse) 对齐，
        // 否则秒级 chat 消息永远排在毫秒级过程流前面，时间线整体错乱。
        const msg: ChatMsg = { ...m, ts: m.ts ? Number(m.ts) * 1000 : Date.now() };
        setMessages((prev) => {
          // B2 防重复：
          // - 通用：同 role+text 且 ts 相近（±3s）→ 跳过（SSE 重连补发 / outbox 补发）。
          // - 用户消息特例：只要已存在同 role+text 的消息就跳过（兜底迟到的 user 回声，
          //   因其 ts 可能远晚于乐观渲染的那条，±3s 兜不住）。
          if (
            prev.some(
              (x) =>
                x.role === msg.role &&
                x.text === msg.text &&
                (msg.role === "user" || Math.abs(x.ts - msg.ts) < 3000)
            )
          ) {
            return prev;
          }
          // B2 终态补全：带 steps 的 agent 终态消息，替换「最后一条无 steps」的
          // agent 消息（重连后终态补发场景）。正常多轮一律追加——旧逻辑
          // 「全局只保留一条 agent」会误杀多轮对话并把新结论错位到第一轮位置。
          if (msg.role === "agent" && msg.extra?.steps?.length) {
            const idx = prev.map((x) => x.role).lastIndexOf("agent");
            if (idx >= 0 && !(prev[idx].extra?.steps?.length)) {
              const next = prev.slice();
              next[idx] = msg;
              return next;
            }
          }
          return [...prev.slice(-300), msg];
        });
        // 运行结束：agent 轮已携带完整 steps，清掉 live 过程流，改由 agent turn 渲染，避免重复
        if (m.role === "agent" && m.extra?.steps?.length) {
          clearProcessLogs();
        }
      } catch {
        /* ignore */
      }
    });
    // debug 事件：内核旁路调试日志（task 下发 / LLM prompt / react），进右侧日志窗口
    es.addEventListener("debug", (ev) => {
      try {
        const m = JSON.parse((ev as MessageEvent).data) as {
          ts?: number; text?: string; extra?: { kind?: string; payload?: Record<string, unknown> };
        };
        pushDebugLog({
          ts: m.ts ? Number(m.ts) * 1000 : Date.now(),
          kind: m.extra?.kind || "debug",
          role: (m.extra?.payload as { role?: string } | undefined)?.role,
          title: m.text || m.extra?.kind || "debug",
          payload: m.extra?.payload || {},
        });
      } catch {
        /* ignore */
      }
    });
    // thinking 事件：对话区实时「深度思考」块（推理模型真实思考链，按 id 增量 upsert）
    es.addEventListener("thinking", (ev) => {
      try {
        const m = JSON.parse((ev as MessageEvent).data) as {
          ts?: number; role?: string; content?: string; model?: string; id?: string;
        };
        upsertProcessLog({
          type: "thinking",
          id: m.id,
          ts: m.ts ? Number(m.ts) * 1000 : Date.now(),
          role: m.role,
          model: m.model,
          content: m.content || "",
        });
      } catch {
        /* ignore */
      }
    });
    // toolcall 事件：对话区实时「工具调用」卡片（name + 参数 + 结果）
    es.addEventListener("toolcall", (ev) => {
      try {
        const m = JSON.parse((ev as MessageEvent).data) as {
          ts?: number; role?: string; name?: string; arguments?: string;
          result?: string; model?: string; id?: string;
        };
        // 按 id upsert：与 live 快照回放的同一条工具卡原地更新，不重复成两张
        upsertProcessLog({
          type: "tool_call",
          id: m.id,
          ts: m.ts ? Number(m.ts) * 1000 : Date.now(),
          role: m.role,
          model: m.model,
          name: m.name,
          arguments: m.arguments,
          result: m.result,
        });
      } catch {
        /* ignore */
      }
    });
    // S2 审批卡：顶置在输入框上方，按 approval_id upsert（多张卡纵向堆叠）
    es.addEventListener("approval", (ev) => {
      try {
        const card = JSON.parse((ev as MessageEvent).data) as ApprovalCardInfo;
        setPendingApprovals((prev) => {
          const idx = prev.findIndex((a) => a.approval_id === card.approval_id);
          if (idx >= 0) {
            const next = prev.slice();
            next[idx] = card;
            return next;
          }
          return [...prev, card];
        });
      } catch {
        /* ignore */
      }
    });
    // S2 决议 / 超时 / 取消：收起对应卡（时间线里随后出现带结果的 toolcall 卡）
    es.addEventListener("approval_resolved", (ev) => {
      try {
        const m = JSON.parse((ev as MessageEvent).data) as { approval_id: string };
        setPendingApprovals((prev) => prev.filter((a) => a.approval_id !== m.approval_id));
      } catch {
        /* ignore */
      }
    });
    // message 事件：对话区实时「口播」气泡（模型每轮自然语言结论，按 id 增量 upsert，与思考一起流式）
    es.addEventListener("message", (ev) => {
      try {
        const m = JSON.parse((ev as MessageEvent).data) as {
          ts?: number; role?: string; content?: string; model?: string; id?: string;
        };
        upsertProcessLog({
          type: "message",
          id: m.id,
          ts: m.ts ? Number(m.ts) * 1000 : Date.now(),
          role: m.role,
          model: m.model,
          content: m.content || "",
        });
      } catch {
        /* ignore */
      }
    });
    es.addEventListener("status", (ev) => {
      try {
        const s = JSON.parse((ev as MessageEvent).data) as {
          running: boolean; task_id: string; project_id: string;
        };
        const wasRunning = prevRunningRef.current;
        prevRunningRef.current = s.running;
        setRunning(s.running);
        setSnapshot((prev) => ({ ...prev, running: s.running, task_id: s.task_id, project_id: s.project_id }));
        // 新 run 启动（false→true）：清上一轮残留过程流。block_id 已带 run 级后缀，
        // 这里是双保险——链式续跑不经 sendMessage，没有别的清理时机
        if (!wasRunning && s.running) {
          clearProcessLogs();
        }
        if (s.task_id) {
          // 仅在「未绑定具体会话」（全局/新建对话视图，currentTaskId 为空）时，
          // 才随 status 自动切到运行中的 task 并回拉历史；一旦已绑定到某个具体会话，
          // 不再被其他 task 的 status 抢走视图，根治「切到旧会话却显示新会话回答」的串台。
          if (!currentTaskIdRef.current && s.task_id !== currentTaskIdRef.current) {
            setCurrentTaskId(s.task_id);
            loadHistory(s.task_id);
          }
        }
        if (!s.running) {
          // 任务终态：不允许还有卡悬着（后端已置 cancelled，前端兜底清空）
          setPendingApprovals([]);
          refreshTasks();
          // 排队兜底接力：注入被拒而排队的消息，本轮结束自动作为新消息发出
          // （正常注入路径不排队，这里只处理兜底队列；同步改 ref 防 SSE 连帧双发）
          const q = queuedRef.current;
          // 防抖延时：给后端收尾补投让路（它若已开跑则 running=true，这里直接放弃）；
          // 同时一轮只接力一条，等下一轮 status 确认后再续
          if (q.length && pumpTimerRef.current == null) {
            pumpTimerRef.current = window.setTimeout(() => {
              pumpTimerRef.current = null;
              if (runningRef.current) return;
              const cur = queuedRef.current;
              if (!cur.length) return;
              queuedRef.current = cur.slice(1);
              setQueuedMessages(queuedRef.current);
              sendMessageRef.current?.(cur[0]);
            }, 600);
          }
        }
      } catch {
        /* ignore */
      }
    });
    es.addEventListener("skills", (ev) => {
      try {
        const d = JSON.parse((ev as MessageEvent).data) as { skills: SkillInfo[] };
        setSnapshot((prev) => ({ ...prev, skills: d.skills || [] }));
      } catch {
        /* ignore */
      }
    });
    es.addEventListener("world", (ev) => {
      try {
        const d = JSON.parse((ev as MessageEvent).data) as { world: RuntimeSnapshot["world"] };
        setSnapshot((prev) => ({ ...prev, world: d.world || { ok: false } }));
      } catch {
        /* ignore */
      }
    });
    es.addEventListener("collected", (ev) => {
      try {
        const d = JSON.parse((ev as MessageEvent).data) as { collected: RuntimeSnapshot["collected"] };
        setSnapshot((prev) => ({ ...prev, collected: d.collected || { items: [], total: 0 } }));
      } catch {
        /* ignore */
      }
    });
    es.addEventListener("trajectory_append", (ev) => {
      try {
        const d = JSON.parse((ev as MessageEvent).data) as { step: Record<string, unknown> };
        setSnapshot((prev) => ({
          ...prev,
          trajectory: [...prev.trajectory.slice(-200), d.step],
        }));
      } catch {
        /* ignore */
      }
    });
  }, [refreshTasks]);

  const disconnectStream = useCallback(() => {
    if (esRef.current) {
      esRef.current.close();
      esRef.current = null;
    }
  }, []);

  const setFullAccessForTask = useCallback((taskId: string, val: boolean) => {
    setFullAccessByTask((prev) => ({ ...prev, [taskId]: val }));
    // 持久化到该 task 的 task.json（跟随当前任务）；空 taskId（新建对话未发首条前）无法落盘，跳过
    if (taskId) taskApi.updateState(taskId, { full_access: val }).catch(() => {});
  }, []);

  const sendMessage = useCallback(async (text: string, fullAccess?: boolean, model?: string) => {
    const content = text.trim();
    if (!content || runningRef.current) return;
    // 乐观渲染用户消息：发送瞬间上屏。后端不回传 user 消息（仅 push agent/system/debug），
    // 故无重复；即便有迟到的 SSE 回声，B2 去重也会吞掉（见 chat 监听器）。
    pushMessage({ role: "user", text: content, ts: Date.now() });
    // 新的一轮：清空上一轮残留的思考/工具调用过程流
    clearProcessLogs();
    setRunning(true);
    try {
      const history = messagesRef.current.map((m) => ({ role: m.role, content: m.text }));
      history.push({ role: "user", content });
      const res = await runtimeApi.chat({
        messages: history,
        task_id: currentTaskIdRef.current,
        max_steps: maxSteps,
        full_access: fullAccess,
        ...(model ? { model } : {}),
      });
      const tid = (res.data as { task_id?: string })?.task_id;
      if (tid) {
        setCurrentTaskId(tid);
        // 首条消息新建任务时若开了完全访问，落盘到新 task 的 task.json（弥补空 taskId 时写不进的坑）
        if (fullAccess) taskApi.updateState(tid, { full_access: true }).catch(() => {});
      }
    } catch (err) {
      // 透出后端拒绝/失败原因（如「已有任务在运行（task_id=...）」），便于前端展示排队/拒绝原因。
      const _e = err as { response?: { data?: { error?: string } }; message?: string };
      const _msg = _e?.response?.data?.error || _e?.message || "请求失败，请重试";
      pushMessage({ role: "system", text: `（${_msg}）`, ts: Date.now() });
    } finally {
      // running 由 SSE status 事件驱动，不在这里清
    }
  }, [maxSteps, pushMessage]);
  sendMessageRef.current = sendMessage;

  const stopTask = useCallback(() => {
    runtimeApi.stop().catch(() => {});
    setRunning(false);
  }, []);

  // 立即发送（barge-in）：打断同一 task 的当前轮并立即发出。与 sendMessage 的差别：
  // 允许在 running 时调用、带 force=true；乐观渲染一致。running 态由 SSE 驱动，这里不碰。
  const forceSendMessage = useCallback(async (text: string, fullAccess?: boolean, model?: string) => {
    const content = text.trim();
    if (!content) return;
    pushMessage({ role: "user", text: content, ts: Date.now() });
    clearProcessLogs();
    try {
      const history = messagesRef.current.map((m) => ({ role: m.role, content: m.text }));
      history.push({ role: "user", content });
      const res = await runtimeApi.chat({
        messages: history,
        task_id: currentTaskIdRef.current,
        max_steps: maxSteps,
        full_access: fullAccess,
        ...(model ? { model } : {}),
        force: true,
      });
      const tid = (res.data as { task_id?: string })?.task_id;
      if (tid) setCurrentTaskId(tid);
    } catch (err) {
      const _e = err as { response?: { data?: { error?: string } }; message?: string };
      const _msg = _e?.response?.data?.error || _e?.message || "请求失败，请重试";
      pushMessage({ role: "system", text: `（${_msg}）`, ts: Date.now() });
    }
  }, [maxSteps, pushMessage]);

  // M8 软注入：运行中插入人类纠偏，不打断当前 turn，主 agent 下一请求边界可见。
  // 成功后后端经 SSE 推一条 system 消息「💬 已注入指示：…」自动上屏。
  // 返回是否成功——失败（收尾窗口/无运行体被 409）由调用方转排队兜底。
  const injectMessage = useCallback(async (text: string): Promise<boolean> => {
    const content = text.trim();
    if (!content) return false;
    try {
      await runtimeApi.inject({ text: content, task_id: currentTaskIdRef.current });
      return true;
    } catch (err) {
      const e = err as { response?: { data?: { error?: string } }; message?: string };
      const msg = e?.response?.data?.error || e?.message || "注入失败";
      pushMessage({ role: "system", text: `（注入失败：${msg}）`, ts: Date.now() });
      return false;
    }
  }, [pushMessage]);

  // 排队兜底：仅注入被拒时入队（ref 权威 + state 镜像），轮次结束由接力自动发出。
  // 正常注入路径不排队——避免「已注入又排队/立即再发一次」的双重投递混乱。
  const enqueueMessage = useCallback((text: string) => {
    const content = text.trim();
    if (!content) return;
    queuedRef.current = [...queuedRef.current, content];
    setQueuedMessages(queuedRef.current);
  }, []);

  const removeQueuedAt = useCallback((index: number) => {
    queuedRef.current = queuedRef.current.filter((_, i) => i !== index);
    setQueuedMessages(queuedRef.current);
  }, []);

  // 运行中发送入口：先注入本轮；被拒才排队兜底（轮末自动接力，见 status 监听）。
  const injectOrQueue = useCallback((text: string) => {
    const content = text.trim();
    if (!content) return;
    void injectMessage(content).then((ok) => {
      if (!ok) enqueueMessage(content);
    });
  }, [injectMessage, enqueueMessage]);

  // S2 审批决议：REST 决议；乐观移卡（approval_resolved 事件与 /live 兜底对齐）
  const decideApproval = useCallback(async (approvalId: string, action: "approve" | "deny", remember?: boolean) => {
    setPendingApprovals((prev) => prev.filter((a) => a.approval_id !== approvalId));
    try {
      await approvalsApi.decide(approvalId, action, remember);
    } catch (err) {
      const e = err as { response?: { data?: { error?: string } }; message?: string };
      const msg = e?.response?.data?.error || e?.message || "请重试";
      pushMessage({ role: "system", text: `（审批提交失败：${msg}）`, ts: Date.now() });
    }
  }, [pushMessage]);

  // ── 右侧 dock 窗口动作（浏览器式页签） ──
  const openDockView = useCallback((view: DockView) => {
    setDockOpen(true);
    setDockWin((w) => {
      const exist = w.tabs.find((t) => t.view === view);
      if (exist) return { ...w, activeId: exist.id };
      const id = `dock-${++dockTabSeq}`;
      return { tabs: [...w.tabs, { id, view }], activeId: id };
    });
  }, []);
  const openDock = useCallback((tab: DockTab, previewPath?: string) => {
    openDockView(tab);
    setDockPreviewPath(previewPath || "");
  }, [openDockView]);
  // 打开窗口并停在主页（无主页页签则新建）
  const openDockMenu = useCallback(() => openDockView("home"), [openDockView]);
  const closeDock = useCallback(() => setDockOpen(false), []);
  // + 新建页签：暂时默认主页
  const newDockTab = useCallback(() => openDockView("home"), [openDockView]);
  const activateDockTab = useCallback((id: string) => {
    setDockWin((w) => ({ ...w, activeId: id }));
  }, []);
  const closeDockTab = useCallback((id: string) => {
    setDockWin((w) => {
      const tabs = w.tabs.filter((t) => t.id !== id);
      if (!tabs.length) return { tabs: [], activeId: "" };
      const activeId = w.activeId === id ? tabs[tabs.length - 1].id : w.activeId;
      return { tabs, activeId };
    });
  }, []);
  // 关掉最后一个页签 = 整窗收起（浏览器语义）
  useEffect(() => {
    if (dockOpen && dockWin.tabs.length === 0) setDockOpen(false);
  }, [dockOpen, dockWin.tabs.length]);
  // 主页点入口：当前页签原地跳转到功能视图
  const navigateDockTab = useCallback((view: DockTab) => {
    setDockWin((w) => ({ ...w, tabs: w.tabs.map((t) => (t.id === w.activeId ? { ...t, view } : t)) }));
  }, []);
  const bumpArtifactsVersion = useCallback(() => setArtifactsVersion((v) => v + 1), []);

  const deleteTask = useCallback(async (taskId: string) => {
    try {
      await taskApi.remove(taskId);
    } catch {
      /* 忽略瞬时错误 */
    }
    if (currentTaskId === taskId) {
      setCurrentTaskId("");
      try { localStorage.setItem("omniagent.currentTaskId", ""); } catch { /* ignore */ }
      clearMessages();
    }
    setTaskTitles((prev) => {
      const next = { ...prev };
      delete next[taskId];
      persistTitles(next);
      return next;
    });
    await refreshTasks();
  }, [currentTaskId, clearMessages, refreshTasks, setCurrentTaskId]);

  const renameTask = useCallback(async (taskId: string, title: string) => {
    const objective = title.trim();
    if (!objective) return;
    try {
      await taskApi.rename(taskId, objective);
    } catch {
      /* 忽略瞬时错误 */
    }
    // 用户自定义标题优先：覆盖 taskTitles（持久化）并同步 objectives（避免回退到服务端旧值）
    setTaskTitles((prev) => {
      const next = { ...prev, [taskId]: objective };
      persistTitles(next);
      return next;
    });
    setTaskObjectives((prev) => ({ ...prev, [taskId]: objective }));
  }, []);

  const value = useMemo<TaskStoreValue>(
    () => ({
      view, setView,
      tasks, projects, currentTaskId, setCurrentTaskId, selectTask, refreshTasks,
      running,
      messages, pushMessage, clearMessages,
      snapshot, setSnapshot,
      connectStream, disconnectStream, sendMessage, forceSendMessage, injectOrQueue, removeQueuedAt, queuedMessages, injectMessage, stopTask,
      maxSteps, setMaxSteps,
      fullAccessByTask, setFullAccessForTask,
      pendingApprovals, decideApproval,
      taskTitles,
      taskObjectives,
      taskProjects,
      debugLogs, pushDebugLog, clearDebugLogs,
      processLogs, pushProcessLog, clearProcessLogs,
      deleteTask, renameTask,
      dockOpen, dockTabs: dockWin.tabs, dockActiveId: dockWin.activeId, dockPreviewPath,
      openDock, openDockMenu, closeDock, newDockTab, closeDockTab, activateDockTab, navigateDockTab, setDockPreviewPath,
      artifactsVersion, bumpArtifactsVersion,
    }),
    [view, tasks, projects, currentTaskId, selectTask, refreshTasks, running, messages, pushMessage, clearMessages, snapshot, connectStream, disconnectStream, sendMessage, forceSendMessage, injectOrQueue, removeQueuedAt, queuedMessages, injectMessage, stopTask, maxSteps, deleteTask, renameTask, taskObjectives, taskProjects, debugLogs, pushDebugLog, clearDebugLogs, processLogs, pushProcessLog, clearProcessLogs, pendingApprovals, decideApproval, dockOpen, dockWin, dockPreviewPath, openDock, openDockMenu, closeDock, newDockTab, closeDockTab, activateDockTab, navigateDockTab, setDockPreviewPath, artifactsVersion, bumpArtifactsVersion]
  );

  return <TaskStoreContext.Provider value={value}>{children}</TaskStoreContext.Provider>;
}

export function useTaskStore(): TaskStoreValue {
  const ctx = useContext(TaskStoreContext);
  if (!ctx) throw new Error("useTaskStore 必须在 TaskStoreProvider 内使用");
  return ctx;
}
