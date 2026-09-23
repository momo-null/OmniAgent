// 全局 UI 状态：当前视图、task 列表、当前 task、消息流、SSE 连接
// 单页 shell，视图通过 state 切换（无路由跳转）
// 使用 React Context 实现，不引入额外依赖。
import React, { createContext, useContext, useMemo, useRef, useState, useCallback, useEffect } from "react";
import { runtimeApi, taskApi, projectApi, settingsApi } from "../api/client";
import type { ChatMsg, RuntimeSnapshot, SkillInfo, ProcessItem } from "../types";

export interface DebugLog {
  ts: number;
  kind: string;
  role?: string;
  title: string;
  payload: Record<string, unknown>;
}

export type MainView = "chat" | "skills" | "settings";

// SSE debug 通道 memory_updated 事件载荷（Curator 蒸馏/合并后推送，右栏轻提示用）
export interface MemoryHint {
  distilled: number;
  merged: number;
  ts: number;
}

interface TaskStoreValue {
  view: MainView;
  setView: (v: MainView) => void;

  tasks: string[];
  projects: string[];
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
  sendMessage: (text: string, fullAccess?: boolean) => Promise<void>;
  // M8 软注入：运行中向 agent 插话（不打断当前步骤，下一轮可见）；失败回显系统消息
  injectMessage: (text: string) => Promise<void>;
  stopTask: () => void;

  maxSteps: number;
  setMaxSteps: (n: number) => void;

  // 完全访问按 task 记忆：每个 task 各自记住开关，切换任务时恢复，不串到其他 task
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

  // K1：记忆更新轻提示（Curator 蒸馏/合并后由 SSE 推送，数秒后自动淡出）
  memoryHint: MemoryHint | null;
  memorySignal: number; // 每次记忆更新自增，供 Memory Tab 作为刷新依赖
}

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
  const [debugLogs, setDebugLogs] = useState<DebugLog[]>([]);
  // 对话区实时过程流（思考 + 工具调用），与右栏 debug 日志解耦
  const [processLogs, setProcessLogs] = useState<ProcessItem[]>([]);
  // K1：记忆更新轻提示（SSE 推送后短暂显示，自动淡出）
  const [memoryHint, setMemoryHint] = useState<MemoryHint | null>(null);
  const [memorySignal, setMemorySignal] = useState<number>(0);
  const memoryHintTimerRef = useRef<number | null>(null);

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
      };
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
      // 按 task 记忆的完全访问默认值：从服务端 task.json 回填（刷新页面后从磁盘恢复）
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
        // K1：Curator 蒸馏/合并完成后推 memory_updated，触发右栏轻提示 + Memory Tab 统计刷新
        if (m.extra?.kind === "memory_updated") {
          const p = (m.extra?.payload as { distilled?: number; merged?: number }) || {};
          setMemoryHint({
            distilled: Number(p.distilled) || 0,
            merged: Number(p.merged) || 0,
            ts: Date.now(),
          });
          setMemorySignal((n) => n + 1);
          if (memoryHintTimerRef.current) window.clearTimeout(memoryHintTimerRef.current);
          memoryHintTimerRef.current = window.setTimeout(() => setMemoryHint(null), 5000);
        }
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
        setRunning(s.running);
        setSnapshot((prev) => ({ ...prev, running: s.running, task_id: s.task_id, project_id: s.project_id }));
        if (s.task_id) {
          // 仅在「未绑定具体会话」（全局/新建对话视图，currentTaskId 为空）时，
          // 才随 status 自动切到运行中的 task 并回拉历史；一旦已绑定到某个具体会话，
          // 不再被其他 task 的 status 抢走视图，根治「切到旧会话却显示新会话回答」的串台。
          if (!currentTaskIdRef.current && s.task_id !== currentTaskIdRef.current) {
            setCurrentTaskId(s.task_id);
            loadHistory(s.task_id);
          }
        }
        if (!s.running) refreshTasks();
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
    // 持久化到该 task 的 task.json（跟随 task 写磁盘，刷新不丢）；空 taskId（新建前）跳过
    if (taskId) taskApi.updateState(taskId, { full_access: val }).catch(() => {});
  }, []);

  const sendMessage = useCallback(async (text: string, fullAccess?: boolean) => {
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
      });
      const tid = (res.data as { task_id?: string })?.task_id;
      if (tid) setCurrentTaskId(tid);
    } catch (err) {
      // 透出后端拒绝/失败原因（如「已有任务在运行（task_id=...）」），便于前端展示排队/拒绝原因。
      const _e = err as { response?: { data?: { error?: string } }; message?: string };
      const _msg = _e?.response?.data?.error || _e?.message || "请求失败，请重试";
      pushMessage({ role: "system", text: `（${_msg}）`, ts: Date.now() });
    } finally {
      // running 由 SSE status 事件驱动，不在这里清
    }
  }, [maxSteps, pushMessage]);

  const stopTask = useCallback(() => {
    runtimeApi.stop().catch(() => {});
    setRunning(false);
  }, []);

  // M8 软注入：运行中插入人类纠偏，不打断当前 turn，主 agent 下一轮可见。
  // 成功后后端经 SSE 推一条 system 消息「💬 已注入指示：…」自动上屏；这里只负责发请求。
  const injectMessage = useCallback(async (text: string) => {
    const content = text.trim();
    if (!content) return;
    try {
      await runtimeApi.inject({ text: content, task_id: currentTaskIdRef.current });
    } catch (err) {
      const e = err as { response?: { data?: { error?: string } }; message?: string };
      const msg = e?.response?.data?.error || e?.message || "注入失败";
      pushMessage({ role: "system", text: `（注入失败：${msg}）`, ts: Date.now() });
    }
  }, [pushMessage]);

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
      connectStream, disconnectStream, sendMessage, injectMessage, stopTask,
      maxSteps, setMaxSteps,
      fullAccessByTask, setFullAccessForTask,
      taskTitles,
      taskObjectives,
      debugLogs, pushDebugLog, clearDebugLogs,
      processLogs, pushProcessLog, clearProcessLogs,
      deleteTask, renameTask,
      memoryHint, memorySignal,
    }),
    [view, tasks, projects, currentTaskId, selectTask, refreshTasks, running, messages, pushMessage, clearMessages, snapshot, connectStream, disconnectStream, sendMessage, injectMessage, stopTask, maxSteps, deleteTask, renameTask, taskObjectives, debugLogs, pushDebugLog, clearDebugLogs, processLogs, pushProcessLog, clearProcessLogs, memoryHint, memorySignal]
  );

  return <TaskStoreContext.Provider value={value}>{children}</TaskStoreContext.Provider>;
}

export function useTaskStore(): TaskStoreValue {
  const ctx = useContext(TaskStoreContext);
  if (!ctx) throw new Error("useTaskStore 必须在 TaskStoreProvider 内使用");
  return ctx;
}
