import { Fragment, memo, useCallback, useEffect, useLayoutEffect, useRef, useState, useMemo, type MouseEvent as ReactMouseEvent, type ReactNode } from "react";
import {
  Box,
  Paper,
  TextField,
  Typography,
  Stack,
  Switch,
  Chip,
  IconButton,
  Button,
  Checkbox,
  FormControlLabel,
  Alert,
  Popover,
} from "@mui/material";
import SendIcon from "@mui/icons-material/Send";
import StopIcon from "@mui/icons-material/Stop";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import ExpandLessIcon from "@mui/icons-material/ExpandLess";
import BugReportIcon from "@mui/icons-material/BugReport";
import ContentCopyIcon from "@mui/icons-material/ContentCopy";
import CheckIcon from "@mui/icons-material/Check";
import PsychologyIcon from "@mui/icons-material/Psychology";
import TerminalIcon from "@mui/icons-material/Terminal";
import WarningAmberRounded from "@mui/icons-material/WarningAmberRounded";
import FolderRounded from "@mui/icons-material/FolderRounded";
import PublicRounded from "@mui/icons-material/PublicRounded";
import { useTaskStore } from "../../store/taskStore.tsx";
import ScrollArea, { THUMB_RIGHT, THUMB_WIDTH } from "../../components/ScrollArea.tsx";
import Markdown from "../../components/Markdown.tsx";
import ModelPicker from "../../components/ModelPicker.tsx";
import DebugPanel from "../../components/DebugPanel.tsx";
import type { ChatMsg, ProcessItem, ApprovalCardInfo, ModelsIndex } from "../../types";
import { characterApi, modelCatalogApi, settingsApi } from "../../api/client";

// 选中模型的本地兜底：后端目录为准，本地只用于首屏即时显示（切换后由后端 defaults 持久化）
const MODEL_SEL_KEY = "omni.selected_model";

// S2 审批卡风险标签（通用三词 + 网络，零工具特判——前端不认识具体工具）
const RISK_LABEL: Record<string, string> = {
  exec: "命令执行",
  actuate: "键鼠操作",
  network: "网络访问",
  write: "写入文件",
};

// 顶置审批卡（§6.3）：参数等宽展示、可折叠完整 JSON；actuate 默认勾选任务级记忆
function ApprovalCardView({ card, onDecide }: {
  card: ApprovalCardInfo;
  onDecide: (id: string, action: "approve" | "deny", remember?: boolean) => void;
}) {
  const [remember, setRemember] = useState(card.risk === "actuate");
  const [argsOpen, setArgsOpen] = useState(false);
  const argsText = useMemo(() => {
    try {
      return JSON.stringify(card.arguments ?? {}, null, 2);
    } catch {
      return String(card.arguments);
    }
  }, [card]);
  const riskLabel = RISK_LABEL[card.risk] ?? "工具调用";
  return (
    <Alert
      severity="warning"
      icon={<WarningAmberRounded fontSize="small" />}
      sx={{ mb: 1, flexShrink: 0, borderRadius: 2, alignItems: "flex-start" }}
    >
      <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 0.5 }}>
        <Typography variant="subtitle2" sx={{ fontWeight: 600 }}>
          {riskLabel} · {card.tool}
        </Typography>
        <Chip size="small" label={card.unit} variant="outlined" sx={{ height: 20 }} />
      </Stack>
      <Box
        sx={{
          maxHeight: argsOpen ? 220 : 72, overflow: "auto", mb: 0.5,
          bgcolor: "action.hover", borderRadius: 1, p: 1,
          fontFamily: "monospace", fontSize: 12, whiteSpace: "pre-wrap",
          wordBreak: "break-all",
        }}
      >
        {argsText}
      </Box>
      <Button size="small" sx={{ minWidth: 0, px: 0.5, mb: 0.5 }}
        onClick={() => setArgsOpen((v) => !v)}
        startIcon={argsOpen ? <ExpandLessIcon /> : <ExpandMoreIcon />}>
        {argsOpen ? "收起参数" : "展开完整参数"}
      </Button>
      <Stack direction="row" justifyContent="space-between" alignItems="center" sx={{ flexWrap: "wrap", gap: 0.5 }}>
        <FormControlLabel
          control={<Checkbox size="small" checked={remember}
            onChange={(e) => setRemember(e.target.checked)} />}
          label={<Typography variant="caption">本任务内不再询问同类操作</Typography>}
          sx={{ mr: 0 }}
        />
        <Stack direction="row" spacing={1}>
          <Button size="small" onClick={() => onDecide(card.approval_id, "deny", false)}>拒绝</Button>
          <Button size="small" variant="contained" color="warning"
            onClick={() => onDecide(card.approval_id, "approve", remember)}>允许</Button>
        </Stack>
      </Stack>
      <Typography variant="caption" sx={{ color: "text.secondary", display: "block" }}>
        {card.wait_seconds > 0
          ? `${Math.round(card.wait_seconds)} 秒未处理将自动拒绝`
          : "将一直等待人工决议"}
      </Typography>
    </Alert>
  );
}

function senderKey(it: ChatMsg | ProcessItem): string | null {
  // 同一角色连续内容归为一组（仅组首显示一次角色标签）：
  // - 过程流（思考 / 工具调用 / 口播）全部归属 agent，**不再打断分组**
  //   ——旧实现让 thinking/tool_call 返回 null，会把它后面的 agent 回答误判为新组，
  //     导致同一轮出现两个「agent」标签（用户反馈）。
  // - 历史消息里的 assistant 与实时的 agent 视为同一发送者。
  if (it && "type" in it) return "agent";
  const r = (it as ChatMsg).role ?? null;
  return r === "assistant" ? "agent" : r;
}

// 完全访问确认弹窗：Codex 风格的能力卡片（图标方块 + 标题 + 说明 + 状态标签）
function CapabilityRow({ icon, label, desc, tag, tagTone = "default" }: {
  icon: ReactNode;
  label: string;
  desc: string;
  tag?: string;
  tagTone?: "default" | "warning";
}) {
  return (
    <Box sx={{ display: "flex", gap: 1.5, alignItems: "flex-start" }}>
      <Box
        sx={{
          width: 40, height: 40, flexShrink: 0, borderRadius: 1.5,
          display: "flex", alignItems: "center", justifyContent: "center",
          bgcolor: "action.hover",
          color: tagTone === "warning" ? "warning.main" : "text.secondary",
        }}
      >
        {icon}
      </Box>
      <Box sx={{ flex: 1, minWidth: 0 }}>
        <Box sx={{ display: "flex", alignItems: "center", gap: 0.75, flexWrap: "wrap" }}>
          <Typography variant="subtitle2" sx={{ fontWeight: 600 }}>{label}</Typography>
          {tag && (
            <Chip size="small" label={tag} color={tagTone === "warning" ? "warning" : "default"} variant="outlined" />
          )}
        </Box>
        <Typography variant="caption" sx={{ color: "text.secondary", display: "block", lineHeight: 1.5 }}>
          {desc}
        </Typography>
      </Box>
    </Box>
  );
}

// 以下消息类组件全部 memo：SSE 每 0.3s 轮询会让父级高频重渲染，props 不变时
// 直接跳过，已完成的回复不再重新渲染/重解析，DOM 保持稳定（点击不再跳位）。
const MsgRow = memo(function MsgRow({ m, showRole = true, copyable = false }: { m: ChatMsg; showRole?: boolean; copyable?: boolean }) {
  // 回复（实时 agent 与历史 assistant 同源）走 markdown；用户消息不提供复制。
  // 复制图标只挂在「最后一条回复」上（copyable 由时间线下发），中间回复保持干净。
  const isReply = m.role === "agent" || m.role === "assistant";
  return (
    <Box sx={{ mb: showRole ? 1.5 : 0.25, display: "flex", flexDirection: "column", alignItems: m.role === "user" ? "flex-end" : "flex-start" }}>
      {/* 角色标签由分组统一渲染（见下方 timeline.map），此处只保留间距控制 */}
      <Paper
        elevation={0}
        sx={{
          px: 1.5,
          py: 1,
          maxWidth: "85%",
          borderRadius: 2,
          // agent 消息走 markdown 块级排版（容器 pre-wrap 会与 <p> 外距叠加），其余保持原样
          whiteSpace: isReply ? "normal" : "pre-wrap",
          wordBreak: "break-word",
          boxShadow: "none",
          bgcolor: m.role === "user" ? "action.selected" : "transparent",
        }}
      >
        {isReply ? (
          <>
            <Markdown>{m.text}</Markdown>
            {copyable && <CopyButton text={m.text} />}
          </>
        ) : (
          <Typography variant="body2" component="span" sx={{ fontSize: 14 }}>
            {m.text}
          </Typography>
        )}
      </Paper>
    </Box>
  );
});

// ── 对话区实时过程流（思考 + 工具调用），与右栏 debug 日志解耦 ─────────────

// 复制按钮（仅挂在「回复」上：思考块 / 工具调用卡片不提供）
// 独立于鼠标选区，避免流式重渲染把选区冲掉导致复制不到内容。
function CopyButton({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  if (!text) return null;
  const copy = async (e: ReactMouseEvent) => {
    e.stopPropagation();
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // 非安全上下文（http 且非 localhost）下 Clipboard API 不可用，退回 execCommand
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
    }
    setDone(true);
    window.setTimeout(() => setDone(false), 1200);
  };
  return (
    <IconButton
      size="small"
      onClick={copy}
      title={done ? "已复制" : "复制这条回复"}
      aria-label="复制这条回复"
      sx={{
        width: 24, height: 24, mt: 0.25, ml: -0.25,
        color: done ? "success.main" : "text.disabled",
        "&:hover": { color: "text.primary", bgcolor: "transparent" },
      }}
    >
      {done ? <CheckIcon sx={{ fontSize: 15 }} /> : <ContentCopyIcon sx={{ fontSize: 15 }} />}
    </IconButton>
  );
}

const ThinkBlock = memo(function ThinkBlock({ item, streaming }: { item: ProcessItem; streaming?: boolean }) {
  // 深度思考作辅助信息：固定高度 + 内部滚动，标题灰化，不喧宾夺主；不改 chat 气泡样式
  // 流式打字期间展开；一旦停止（结果 / 下一阶段出现）自动收起
  const [open, setOpen] = useState<boolean>(!!streaming);
  const wasStreaming = useRef(false);
  useEffect(() => {
    if (streaming) {
      setOpen(true);
      wasStreaming.current = true;
    } else if (wasStreaming.current) {
      setOpen(false);
      wasStreaming.current = false;
    }
  }, [streaming]);
  const text = item.content || "";
  return (
    <Box sx={{ mb: 1, display: "flex", justifyContent: "flex-start" }}>
      <Paper
        elevation={0}
        sx={{
          maxWidth: "92%", pl: 1.25, pr: 0, py: 0.5, borderRadius: 1.5,
          bgcolor: "rgba(0,0,0,0.03)", color: "text.secondary", boxShadow: "none",
        }}
      >
        <Stack direction="row" spacing={0.5} alignItems="center" onClick={() => setOpen((o) => !o)}
               sx={{ cursor: "pointer" }}>
          <PsychologyIcon fontSize="small" sx={{ color: "text.disabled" }} />
          <Typography variant="caption" sx={{ fontWeight: 500, color: "text.disabled", letterSpacing: 0.5 }}>
            深度思考
          </Typography>
          {item.model && (
            <Chip size="small" label={item.model} variant="outlined"
                  sx={{ height: 16, fontSize: 10, color: "text.disabled", borderColor: "divider", "& .MuiChip-label": { px: 0.5 } }} />
          )}
          <Box sx={{ ml: "auto" }}>{open
            ? <ExpandLessIcon fontSize="small" sx={{ color: "text.disabled" }} />
            : <ExpandMoreIcon fontSize="small" sx={{ color: "text.disabled" }} />}</Box>
        </Stack>
        {open && (
          <ScrollArea maxHeight={200} sx={{ mt: 0.5 }}>
            <Typography variant="body2" sx={{ fontSize: 13, color: "text.secondary", pr: 1.5, whiteSpace: "pre-wrap", wordBreak: "break-word", display: "block" }}>
              {text}
            </Typography>
          </ScrollArea>
        )}
      </Paper>
    </Box>
  );
});

// 真流式「口播」气泡（问题1/3）：模型每轮自然语言结论，与思考块一起在对话区时间线流式呈现
const MessageBubble = memo(function MessageBubble({ item, showRole = true, copyable = false }: { item: ProcessItem; showRole?: boolean; copyable?: boolean }) {
  const text = item.content || "";
  return (
    <Box sx={{ mb: showRole ? 1.5 : 0.25, display: "flex", flexDirection: "column", alignItems: "flex-start" }}>
      {/* 角色标签由分组统一渲染，此处不再单独渲染 */}
      <Paper
        elevation={0}
        sx={{
          px: 1.5, py: 1, maxWidth: "92%", borderRadius: 2,
          wordBreak: "break-word", boxShadow: "none", bgcolor: "transparent",
        }}
      >
        {/* LLM 口播内容按 markdown 渲染 */}
        <Markdown>{text}</Markdown>
        {copyable && <CopyButton text={text} />}
      </Paper>
    </Box>
  );
});

const ToolCallCard = memo(function ToolCallCard({ item }: { item: ProcessItem }) {
  const [open, setOpen] = useState(false);
  const resLen = (item.result || "").length;
  return (
    <Box sx={{ mb: 1, display: "flex", justifyContent: "flex-start" }}>
      {/* 与思考块/回复气泡统一：极淡底 + 无阴影无边框（原 action.hover 灰底像卡片） */}
      <Paper elevation={0} sx={{ maxWidth: "92%", px: 1.25, py: 0.75, borderRadius: 1.5, bgcolor: "rgba(0,0,0,0.03)", boxShadow: "none", border: "none" }}>
        <Stack direction="row" spacing={0.5} alignItems="center" flexWrap="wrap">
          <TerminalIcon fontSize="small" color="action" />
          <Typography variant="caption" sx={{ fontWeight: 600 }}>
            🔧 {item.name}
          </Typography>
          {item.model && (
            <Chip size="small" label={item.model} variant="outlined"
                  sx={{ height: 16, fontSize: 10, "& .MuiChip-label": { px: 0.5 } }} />
          )}
          <IconButton size="small" sx={{ py: 0, ml: "auto" }} onClick={() => setOpen((o) => !o)}>
            {open ? <ExpandLessIcon fontSize="small" /> : <ExpandMoreIcon fontSize="small" />}
          </IconButton>
        </Stack>
        {open && (
          <Box sx={{ mt: 0.5 }}>
            {item.arguments ? (
              <Box sx={{ mb: 0.5 }}>
                <Typography variant="caption" color="text.secondary">参数</Typography>
                <Box component="pre" sx={{ whiteSpace: "pre-wrap", wordBreak: "break-word", fontSize: 12,
                  maxHeight: 200, overflow: "auto", bgcolor: "transparent", p: 0.75, borderRadius: 1, m: 0 }}>
                  {item.arguments}
                </Box>
              </Box>
            ) : (
              <Typography variant="caption" color="text.secondary">（无参数）</Typography>
            )}
            <Typography variant="caption" color="text.secondary">结果{resLen > 600 ? "（已截断）" : ""}</Typography>
            <Box component="pre" sx={{ whiteSpace: "pre-wrap", wordBreak: "break-word", fontSize: 12,
              maxHeight: 260, overflow: "auto", bgcolor: "transparent", p: 0.75, borderRadius: 1, m: 0 }}>
              {item.result}
            </Box>
          </Box>
        )}
      </Paper>
    </Box>
  );
});

// 结构化 agent turn：把「思考 + 工具调用 + 结论」渲染为一个归属 agent 的整体，
// 顺序固定为 思考/工具 在上、结论在中、状态脚注在下（绝不结论在最上）。
const AgentTurn = memo(function AgentTurn({ m, showRole = true, copyable = false }: { m: ChatMsg; showRole?: boolean; copyable?: boolean }) {
  const steps = m.extra?.steps || [];
  const meta = m.extra?.meta;
  // 结论 = LLM 返回内容（B9）：若与最后一条 message step 同源则不重复渲染，
  // 否则以普通 MessageBubble 追加。不再用独立的「最终结果」醒目卡片。
  const lastMsg = [...steps].reverse().find((s) => s.type === "message") as ProcessItem | undefined;
  const showText = m.text && !(lastMsg && (lastMsg.content || "") === m.text) ? m.text : "";
  // 复制图标只挂本 turn 的结论：有独立结论气泡则挂它，否则挂最后一个 message step
  const lastMsgIdx = steps.reduce((acc, s, i) => (s.type === "message" ? i : acc), -1);
  return (
    <Box sx={{ mb: showRole ? 1.5 : 0.25, display: "flex", flexDirection: "column", alignItems: "flex-start" }}>
      {/* 角色标签由分组统一渲染，此处不再单独渲染 */}
      <Box sx={{ width: "100%" }}>
        {steps.map((s, i) => {
          if (s.type === "thinking") return <ThinkBlock key={`a${i}`} item={s as ProcessItem} />;
          if (s.type === "message")
            return <MessageBubble key={`a${i}`} item={s as ProcessItem} copyable={copyable && !showText && i === lastMsgIdx} />;
          if (s.type === "tool_call") return <ToolCallCard key={`a${i}`} item={s as ProcessItem} />;
          return null;
        })}
      </Box>
      {/* B9：最终结果=LLM 返回内容。与最后一条 message step 同源时不重复渲染；不同则普通气泡追加 */}
      {showText ? (
        <MessageBubble
          item={{ type: "message", content: showText, model: lastMsg?.model } as ProcessItem}
          copyable={copyable}
        />
      ) : null}
      {meta ? (
        <Typography variant="caption" color="text.secondary" sx={{ mt: 0.5 }}>
          ✅ 完成：success={meta.success ? "True" : "False"} steps={String(meta.steps)} escalated={String(meta.escalated)} collected={String(meta.collected)}
        </Typography>
      ) : null}
    </Box>
  );
});

export default function Chat() {
  const { messages, running, sendMessage, injectMessage, stopTask, debugLogs, clearDebugLogs, processLogs, currentTaskId, fullAccessByTask, setFullAccessForTask, pendingApprovals, decideApproval, taskObjectives } = useTaskStore();
  const [draft, setDraft] = useState("");
  const [logOpen, setLogOpen] = useState(false);
  // 助手名：从角色卡 frontmatter 解析（缺省 OmniAgent），用于顶栏与消息标签
  const [assistantName, setAssistantName] = useState("OmniAgent");
  useEffect(() => {
    characterApi
      .get()
      .then((r) => setAssistantName(r.data.name || "OmniAgent"))
      .catch(() => {});
  }, []);
  const [confirmAnchorEl, setConfirmAnchorEl] = useState<HTMLElement | null>(null);
  // 全局权限档位（设置›安全与审批）；full_access 档开时所有任务已免审批，per-task 开关冗余
  const [globalSecurityMode, setGlobalSecurityMode] = useState<string>("standard");
  useEffect(() => {
    settingsApi.get().then((r) => {
      const m = (r.data as any)?.security?.mode;
      if (typeof m === "string") setGlobalSecurityMode(m);
    }).catch(() => {});
  }, []);
  const globalFullAccess = globalSecurityMode === "full_access";
  // 完全访问按当前 task 读取（跟随当前任务；切换任务由 refreshTasks 从 task.json 回填各自值）
  const fullAccess = fullAccessByTask[currentTaskId] ?? false;
  const endRef = useRef<HTMLDivElement | null>(null);

  // ── 模型路由：目录来自 ~/.omniagent/models.json（后端分组），本地只缓存选中项 ──
  const [catalog, setCatalog] = useState<ModelsIndex | null>(null);
  const [modelSel, setModelSel] = useState<string>(() => localStorage.getItem(MODEL_SEL_KEY) || "");
  const refreshCatalog = useCallback(() => {
    modelCatalogApi.list().then((r) => {
      const d = r.data as ModelsIndex;
      setCatalog(d);
      // 后端 defaults 为准：本地缓存若指向已删除的条目则回退到后端解析值
      const sel = d?.current?.main?.selection || "";
      if (sel !== modelSel && !(modelSel && !sel)) setModelSel(sel);
    }).catch(() => {});
  }, [modelSel]);
  useEffect(() => { refreshCatalog(); }, []); // eslint-disable-line react-hooks/exhaustive-deps
  const pickModel = (sel: string) => {
    setModelSel(sel);
    localStorage.setItem(MODEL_SEL_KEY, sel);
    // 选中即成为该槽位默认（写回目录）；失败不影响本轮发送（发送时仍按 selection 覆盖）
    modelCatalogApi.setDefault("main", sel).catch(() => {});
  };

  // 对话时间线：messages（user/agent/system）+ 实时过程流（thinking/tool_call）
  // 按 ts 归并，过程流自然落在对应 user 消息与最终 agent 总结之间。
  const timeline = useMemo(() => {
    const items: Array<ChatMsg | ProcessItem> = [...messages, ...processLogs];
    items.sort((a, b) => a.ts - b.ts);
    return items;
  }, [messages, processLogs]);

  // 最后一条「回复」的位置：复制图标只挂在这里，更早的回复不再重复挂图标。
  // 且只在任务执行完成后挂：流式期间（实时口播还在过程流里）、中途停止后残留的
  // 过程流、断流卡 running，都不会出现按钮——避免回答过程中突然多一个图标。
  const lastReplyIdx = useMemo(() => {
    if (running) return -1;
    for (let i = timeline.length - 1; i >= 0; i -= 1) {
      const it = timeline[i];
      if ("type" in it) continue; // 过程流（实时口播/思考/工具）不算「已完成的回复」
      if (it.role === "agent" || it.role === "assistant") return i;
    }
    return -1;
  }, [timeline, running]);

  // 滚动策略（2026-09-27）：贴底跟随 + 任务切换瞬时置底，SSE 流式不打断用户上翻
  const timelineViewportRef = useRef<HTMLDivElement | null>(null);
  const pinnedRef = useRef(true);
  const lastTaskRef = useRef(currentTaskId);

  // 任务切换：瞬时跳到最新消息（无平滑动画），并重置为贴底跟随
  useEffect(() => {
    if (lastTaskRef.current !== currentTaskId) {
      lastTaskRef.current = currentTaskId;
      pinnedRef.current = true;
    }
    const el = timelineViewportRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [currentTaskId]);

  // 用户滚动：底部 80px 内视为「贴底」，离开即暂停自动跟随，滚回底部自动恢复
  useEffect(() => {
    const el = timelineViewportRef.current;
    if (!el) return;
    const onScroll = () => {
      pinnedRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    };
    el.addEventListener("scroll", onScroll);
    return () => el.removeEventListener("scroll", onScroll);
  }, []);

  // 内容更新（含 SSE 流式追加）：仅在贴底时跟随，瞬时滚动不做平滑动画
  useEffect(() => {
    const el = timelineViewportRef.current;
    if (!el || !pinnedRef.current) return;
    el.scrollTop = el.scrollHeight;
  }, [messages, processLogs]);

  // 输入框光标保持（Ctrl+Enter 换行）：受控组件提交新 value 会把光标重置到末尾，
  // 用「挂起光标位 + 提交后 layout effect 回设」可靠恢复（rAF 时序不可靠）。
  const draftTaRef = useRef<HTMLTextAreaElement | null>(null);
  const pendingSelRef = useRef<number | null>(null);
  useLayoutEffect(() => {
    if (pendingSelRef.current != null && draftTaRef.current) {
      draftTaRef.current.selectionStart = draftTaRef.current.selectionEnd = pendingSelRef.current;
      pendingSelRef.current = null;
    }
  }, [draft]);

  // 运行中插话（软注入）：不打断当前步骤，主 agent 下一轮可见
  const handleInject = () => {
    const text = draft.trim();
    if (!text || !running) return;
    injectMessage(text);
    setDraft("");
  };

  const handleSend = () => {
    const text = draft.trim();
    if (!text) return;
    if (running) {
      // 运行中普通消息被禁用，回车/发送键转为软注入（插话）
      handleInject();
      return;
    }
    setDraft("");
    sendMessage(text, fullAccess, modelSel || undefined);
  };

  return (
    <Box sx={{ display: "flex", height: "100%", width: "100%", minHeight: 0, overflow: "hidden", bgcolor: "background.default" }}>
      {/* 左：对话区 */}
      <Box
        sx={{
          flexGrow: 1, minWidth: 0, minHeight: 0,
          display: "flex", flexDirection: "column",
          maxWidth: 1200, mx: "auto", width: "100%", px: 2, pb: 4.5,
          overflow: "hidden",
        }}
      >
        <Stack direction="row" justifyContent="space-between" alignItems="center"
          sx={{ mb: 1, pt: 1, pb: 1, borderBottom: "1px solid", borderColor: "divider", flexShrink: 0 }}>
          {/* 顶栏左侧：只显示当前任务 objective（不存在则为空）；新对话也为空 */}
          <Typography variant="subtitle1" noWrap sx={{ fontWeight: 600 }}>
            {currentTaskId ? (taskObjectives[currentTaskId] || "") : ""}
          </Typography>
          {/* 顶栏右侧：预留模型切换等控制位 */}
        </Stack>

        <Paper
          elevation={0}
          sx={{ flexGrow: 1, minHeight: 0, mb: 1, bgcolor: "transparent", borderRadius: 2, overflow: "hidden", display: "flex", flexDirection: "column" }}
        >
          <ScrollArea sx={{ flexGrow: 1, minHeight: 0 }} viewportRef={timelineViewportRef}>
            {/* 用户气泡右对齐，右内边距 = 滑块占位 + 20px 呼吸间距，避免气泡贴着滚动条 */}
            <Box sx={{ p: 2, pr: `${THUMB_RIGHT + THUMB_WIDTH + 20}px` }}>
          {timeline.length === 0 ? (
            <Typography variant="body2" color="text.secondary" sx={{ textAlign: "center", mt: 4 }}>
              开始和 {assistantName} 对话吧。
            </Typography>
          ) : (
            timeline.map((it, i) => {
              const isLast = i === timeline.length - 1;
              const prev = i > 0 ? timeline[i - 1] : null;
              const curRole = senderKey(it);
              // 同一角色的连续内容（含思考 / 工具调用）归为一组，仅组首渲染一次角色标签。
              // 例如「agent 轮 → 深度思考 → agent 回答」只出现一个 agent 标签。
              const groupStart = curRole !== null && (prev === null || senderKey(prev) !== curRole);

              let node: ReactNode;
              if ("type" in it) {
                if (it.type === "thinking")
                  node = <ThinkBlock item={it} streaming={running && isLast} />;
                else if (it.type === "message")
                  node = <MessageBubble item={it} showRole={groupStart} copyable={i === lastReplyIdx} />;
                else
                  node = <ToolCallCard item={it} />;
              } else if ((it.role === "agent" || it.role === "assistant") && it.extra?.steps?.length) {
                // agent 轮若携带结构化 steps，渲染为「思考 + 工具 + 结论」一体的 agent turn
                node = <AgentTurn m={it} showRole={groupStart} copyable={i === lastReplyIdx} />;
              } else {
                node = <MsgRow m={it} showRole={groupStart} copyable={i === lastReplyIdx} />;
              }

              return (
                <Fragment key={`g${i}`}>
                  {/* 仅 agent 侧显示角色名 Chip；用户消息右对齐 + 底色已可区分，不标「你」 */}
                  {groupStart && (curRole === "agent" || curRole === "assistant") && (
                    <Box sx={{ display: "flex", mb: 0.5 }}>
                      <Chip size="small" label={assistantName}
                        color={curRole === "agent" ? "primary" : "default"} />
                    </Box>
                  )}
                  {node}
                </Fragment>
              );
            })
          )}
            <div ref={endRef} />
            </Box>
          </ScrollArea>
        </Paper>

        {/* S2 审批卡：顶置在输入框上方，不随时间线滚走；决议后收起（§6.3） */}
        {pendingApprovals.length > 0 && (
          <Box sx={{ flexShrink: 0, mb: 1 }}>
            {pendingApprovals.map((card) => (
              <ApprovalCardView key={card.approval_id} card={card} onDecide={decideApproval} />
            ))}
          </Box>
        )}

        {/* 胶囊输入框：大圆角容器；底行左侧=完全访问开关，右侧=发送/停止按钮 */}
        <Box
          sx={{
            border: "1px solid", borderColor: "divider",
            borderRadius: "28px",
            bgcolor: "#2e2e2e",
            p: 1,
            mx: "40px",
            flexShrink: 0,
          }}
        >
          <TextField
            placeholder={running ? "运行中可插话（软注入）：输入后回车或点插话按钮，不打断当前步骤" : `给 ${assistantName} 发消息…`}
            multiline
            minRows={3}
            maxRows={10}
            fullWidth
            value={draft}
            inputRef={draftTaRef}
            onChange={(e) => setDraft(e.target.value)}
            sx={{
              "& .MuiOutlinedInput-root": {
                borderRadius: "22px",
                bgcolor: "transparent",
                // multiline 时 MUI 把 16.5px 14px 的内距挂在 root 上（textarea 自身 multiline padding=0），
                // 不清零则与 textarea 层内距叠加，顶部凭空多 ~20px
                padding: 0,
                "& fieldset": { border: "none" },
                "&:hover fieldset": { border: "none" },
                "&.Mui-focused fieldset": { border: "none" },
              },
              "& .MuiInputLabel-root": { display: "none" },
              // 底部 0：textarea 默认 16.5px 下内距会在胶囊内看起来多空一行
              "& .MuiOutlinedInput-input": { color: "text.primary", padding: "4px 10px 0" },
            }}
            onKeyDown={(e) => {
              // IME 组合态（中文选词中）：Enter 是「确认候选」，keyCode 229——
              // 必须放行给输入法，preventDefault 会打断组合导致文本被拦腰劈开
              if ((e.nativeEvent as KeyboardEvent).isComposing || e.keyCode === 229) return;
              if (e.key === "Enter") {
                if (e.ctrlKey || e.metaKey || e.shiftKey) {
                  // 手动在光标处插入换行；光标恢复走 useLayoutEffect（见 pendingSelRef）
                  e.preventDefault();
                  const el = (e.target as HTMLTextAreaElement) || draftTaRef.current;
                  if (!el) return;
                  const start = el.selectionStart ?? draft.length;
                  const end = el.selectionEnd ?? start;
                  pendingSelRef.current = start + 1;
                  setDraft(draft.slice(0, start) + "\n" + draft.slice(end));
                  return;
                }
                e.preventDefault();
                handleSend();
              }
            }}
          />
          <Box sx={{ display: "flex", alignItems: "center", justifyContent: "space-between", px: 1.25, pt: 0.25 }}>
            <FormControlLabel
              control={
                <Switch
                  size="small"
                  checked={fullAccess || globalFullAccess}
                  disabled={running || globalFullAccess}
                  onChange={(e) => {
                    // 关闭直接生效；开启先在当前位置弹小窗确认风险
                    if (e.target.checked) setConfirmAnchorEl(e.currentTarget);
                    else setFullAccessForTask(currentTaskId, false);
                  }}
                />
              }
              label={globalFullAccess ? "完全访问（已被全局档覆盖）" : "完全访问"}
              sx={{ mr: 0 }}
            />
            <Stack direction="row" spacing={0.5} alignItems="center">
              {/* 模型选择：靠近发送键，只显示模型名（运行中禁止切换） */}
              <ModelPicker
                value={modelSel}
                providers={catalog?.providers ?? []}
                current={catalog?.current?.main}
                disabled={running}
                onChange={pickModel}
              />
              {/* 单按钮上下文逻辑：
                  - 非运行态：始终「发送」（空输入禁用）
                  - 运行态 + 空输入：显示「停止」（红）
                  - 运行态 + 有输入：显示「发送」（点即软注入/插话，不打断当前步骤） */}
              <IconButton
                onClick={running ? (draft.trim() ? handleInject : stopTask) : handleSend}
                disabled={!running && !draft.trim()}
                aria-label={running && !draft.trim() ? "停止" : "发送"}
                title={running && !draft.trim() ? "停止当前任务" : (running ? "插话（软注入）：不打断当前步骤" : "发送")}
                sx={{
                  width: 36, height: 36, flexShrink: 0,
                  bgcolor: running && !draft.trim() ? "error.main" : "primary.main",
                  // 中性主题下 primary 是亮灰底，必须用深色字；停止状态是红色底，仍用白字
                  color: (t) => (running && !draft.trim() ? "#fff" : t.palette.primary.contrastText),
                  "&:hover": { bgcolor: running && !draft.trim() ? "error.dark" : "primary.dark" },
                  "&:disabled": { bgcolor: "action.disabledBackground", color: "action.disabled" },
                }}
              >
                {running && !draft.trim() ? <StopIcon /> : <SendIcon />}
              </IconButton>
            </Stack>
          </Box>
        </Box>
        <Popover
          open={Boolean(confirmAnchorEl)}
          anchorEl={confirmAnchorEl}
          onClose={() => setConfirmAnchorEl(null)}
          anchorOrigin={{ vertical: "top", horizontal: "left" }}
          transformOrigin={{ vertical: "bottom", horizontal: "left" }}
          slotProps={{ paper: { sx: { p: 2, width: 300, maxWidth: "92vw", borderRadius: 2 } } }}
        >
          <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }}>
            <WarningAmberRounded color="warning" fontSize="small" />
            <Typography variant="subtitle2" sx={{ fontWeight: 600 }}>要开启完全访问权限吗？</Typography>
          </Stack>
          <Typography variant="caption" sx={{ color: "text.secondary", display: "block", mb: 1.5 }}>
            开启后，Agent 在<strong>本次任务</strong>中执行<strong>危险动作</strong>时<strong>不再弹出确认卡片</strong>，直接放行。
            关闭时（默认），每个危险动作都需要你点头。本开关即审批总闸。
          </Typography>
          <Stack spacing={1.25} sx={{ mb: 1.5 }}>
            <CapabilityRow icon={<FolderRounded />} label="文件和文件夹" desc="读取、创建、修改或删除任意位置的文件" tag="默认已启用" />
            <CapabilityRow icon={<TerminalIcon />} label="命令执行" desc="在宿主机运行系统命令" tag="默认已启用" />
            <CapabilityRow icon={<PublicRounded />} label="互联网和已连接的应用" desc="访问网站、发送数据并使用已启用的工具" tag="默认已启用" />
          </Stack>
          <Stack direction="row" justifyContent="flex-end" spacing={1}>
            <Button size="small" onClick={() => setConfirmAnchorEl(null)}>取消</Button>
            <Button size="small" color="warning" variant="contained" startIcon={<WarningAmberRounded />}
              onClick={() => { setFullAccessForTask(currentTaskId, true); setConfirmAnchorEl(null); }}>
              确认
            </Button>
          </Stack>
        </Popover>
      </Box>

      {/* 右：信息展示区（公共区域）。默认常驻 30px 纵向图标轨（Debug 置顶）；
          点击展开为 420px 窗口面板，后续文件预览 / 可视化等展示窗口复用此格式。
          Debug 面板是可整体删除的独立模块（components/DebugPanel.tsx + api/debugApi.ts） */}
      {logOpen ? (
        <DebugPanel
          logs={debugLogs}
          onClear={clearDebugLogs}
          onClose={() => setLogOpen(false)}
          taskId={currentTaskId}
        />
      ) : (
        <Box
          sx={{
            width: 30, flexShrink: 0, borderLeft: "1px solid", borderColor: "divider",
            display: "flex", flexDirection: "column", alignItems: "center", pt: 1, minHeight: 0,
          }}
        >
          {/* 信息展示区（公共区域）：常驻纵向图标轨，后续文件预览 / 可视化等展示窗口图标按此格式追加 */}
          <Stack direction="column" spacing={0.5} alignItems="center">
            <IconButton onClick={() => setLogOpen(true)} title="Debug" size="small">
              <BugReportIcon fontSize="small" />
            </IconButton>
            {/* 其它信息展示窗口（文件预览 / 可视化等）：在此追加 IconButton 即可 */}
          </Stack>
        </Box>
      )}
    </Box>
  );
}
