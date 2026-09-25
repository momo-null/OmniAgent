import { Fragment, useEffect, useRef, useState, useMemo, type ReactNode } from "react";
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
  FormControlLabel,
  Alert,
  Popover,
} from "@mui/material";
import SendIcon from "@mui/icons-material/Send";
import StopIcon from "@mui/icons-material/Stop";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import ExpandLessIcon from "@mui/icons-material/ExpandLess";
import ChevronRightIcon from "@mui/icons-material/ChevronRight";
import BugReportIcon from "@mui/icons-material/BugReport";
import PsychologyIcon from "@mui/icons-material/Psychology";
import TerminalIcon from "@mui/icons-material/Terminal";
import WarningAmberRounded from "@mui/icons-material/WarningAmberRounded";
import FolderRounded from "@mui/icons-material/FolderRounded";
import PublicRounded from "@mui/icons-material/PublicRounded";
import { useTaskStore } from "../../store/taskStore.tsx";
import ScrollArea from "../../components/ScrollArea.tsx";
import Markdown from "../../components/Markdown.tsx";
import type { ChatMsg, ProcessItem } from "../../types";
import type { DebugLog } from "../../store/taskStore";

function roleColor(role: string): "primary" | "secondary" | "default" {
  if (role === "agent") return "primary";
  if (role === "user") return "secondary";
  return "default";
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

function MsgRow({ m, showRole = true }: { m: ChatMsg; showRole?: boolean }) {
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
          whiteSpace: m.role === "agent" ? "normal" : "pre-wrap",
          wordBreak: "break-word",
          boxShadow: "none",
          bgcolor: m.role === "user" ? "action.selected" : "transparent",
        }}
      >
        {m.role === "agent" ? (
          <Markdown>{m.text}</Markdown>
        ) : (
          <Typography variant="body2" component="span" sx={{ fontSize: 14 }}>
            {m.text}
          </Typography>
        )}
      </Paper>
    </Box>
  );
}

// ── 对话区实时过程流（思考 + 工具调用），与右栏 debug 日志解耦 ─────────────

function StreamCaret() {
  // 打字机光标：随 running 在流式块末尾闪烁
  return <span style={{ animation: "omni-blink 1s step-start infinite", marginLeft: 1 }}>▍</span>;
}

function ThinkBlock({ item, streaming }: { item: ProcessItem; streaming?: boolean }) {
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
              {text}{streaming && <StreamCaret />}
            </Typography>
          </ScrollArea>
        )}
      </Paper>
    </Box>
  );
}

// 真流式「口播」气泡（问题1/3）：模型每轮自然语言结论，与思考块一起在对话区时间线流式呈现
function MessageBubble({ item, streaming, showRole = true }: { item: ProcessItem; streaming?: boolean; showRole?: boolean }) {
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
        {/* LLM 口播内容按 markdown 渲染；打字机光标置于块外，避免被解析吞掉 */}
        <Markdown>{text}</Markdown>
        {streaming && <StreamCaret />}
      </Paper>
    </Box>
  );
}

function ToolCallCard({ item }: { item: ProcessItem }) {
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
}

// 结构化 agent turn：把「思考 + 工具调用 + 结论」渲染为一个归属 agent 的整体，
// 顺序固定为 思考/工具 在上、结论在中、状态脚注在下（绝不结论在最上）。
function AgentTurn({ m, showRole = true }: { m: ChatMsg; showRole?: boolean }) {
  const steps = m.extra?.steps || [];
  const meta = m.extra?.meta;
  // 结论 = LLM 返回内容（B9）：若与最后一条 message step 同源则不重复渲染，
  // 否则以普通 MessageBubble 追加。不再用独立的「最终结果」醒目卡片。
  return (
    <Box sx={{ mb: showRole ? 1.5 : 0.25, display: "flex", flexDirection: "column", alignItems: "flex-start" }}>
      {/* 角色标签由分组统一渲染，此处不再单独渲染 */}
      <Box sx={{ width: "100%" }}>
        {steps.map((s, i) => {
          if (s.type === "thinking") return <ThinkBlock key={`a${i}`} item={s as ProcessItem} />;
          if (s.type === "message") return <MessageBubble key={`a${i}`} item={s as ProcessItem} />;
          if (s.type === "tool_call") return <ToolCallCard key={`a${i}`} item={s as ProcessItem} />;
          return null;
        })}
      </Box>
      {/* B9：最终结果=LLM 返回内容。与最后一条 message step 同源时不重复渲染；不同则普通气泡追加 */}
      {(() => {
        const lastMsg = [...steps].reverse().find((s) => s.type === "message") as ProcessItem | undefined;
        const showText = m.text && !(lastMsg && (lastMsg.content || "") === m.text) ? m.text : "";
        return showText ? (
          <MessageBubble item={{ type: "message", content: showText, model: lastMsg?.model } as ProcessItem} />
        ) : null;
      })()}
      {meta ? (
        <Typography variant="caption" color="text.secondary" sx={{ mt: 0.5 }}>
          ✅ 完成：success={meta.success ? "True" : "False"} steps={String(meta.steps)} escalated={String(meta.escalated)} collected={String(meta.collected)}
        </Typography>
      ) : null}
    </Box>
  );
}

function kindColor(kind: string): "default" | "primary" | "secondary" | "error" | "success" | "warning" {
  if (kind === "llm_error") return "error";
  if (kind === "llm_response") return "success";
  if (kind === "llm_request") return "primary";
  if (kind === "tool_result") return "primary";
  if (kind === "task_init") return "secondary";
  return "default";
}

function FieldRow({ k, v }: { k: string; v: unknown }) {
  const [open, setOpen] = useState(false);
  const str = typeof v === "string" ? v : JSON.stringify(v, null, 2);
  // 长文本字段（prompt / content / arguments / error 等）默认折叠，点开看全量
  if (typeof v === "string" && v.length > 200) {
    return (
      <Box sx={{ mt: 0.5 }}>
        <Button size="small" sx={{ px: 0, py: 0, minWidth: 0 }} onClick={() => setOpen((o) => !o)}>
          {k} {open ? "收起" : "展开"}
        </Button>
        {open && (
          <Box component="pre" sx={{ whiteSpace: "pre-wrap", wordBreak: "break-word", fontSize: 12, maxHeight: 320, overflow: "auto", bgcolor: "action.hover", p: 1, borderRadius: 1, mt: 0.5 }}>
            {str}
          </Box>
        )}
      </Box>
    );
  }
  return (
    <Typography variant="caption" display="block" sx={{ wordBreak: "break-word" }}>
      <b>{k}:</b> {str}
    </Typography>
  );
}

function LogRow({ log }: { log: DebugLog }) {
  const [expanded, setExpanded] = useState(false);
  const time = new Date(log.ts).toLocaleTimeString();
  return (
    <Box sx={{ mb: 1, p: 1, borderRadius: 1, bgcolor: "background.paper" }}>
      <Stack direction="row" spacing={0.5} alignItems="center" flexWrap="wrap">
        <Chip size="small" label={log.kind} color={kindColor(log.kind)} />
        {log.role && <Chip size="small" label={log.role} variant="outlined" />}
        <Typography variant="caption" color="text.secondary">{time}</Typography>
        <IconButton size="small" sx={{ ml: "auto", py: 0 }} onClick={() => setExpanded((v) => !v)}>
          {expanded ? <ExpandLessIcon /> : <ExpandMoreIcon />}
        </IconButton>
      </Stack>
      <Typography variant="body2" sx={{ mt: 0.5 }}>{log.title}</Typography>
      {expanded && (
        <Box sx={{ mt: 0.5 }}>
          {/* 思考（推理链）置顶高亮，让「每轮 react」的 thought 一眼可见 */}
          {log.kind === "llm_response" && typeof log.payload?.content === "string" && log.payload.content.trim() && (
            <Box sx={{ mb: 0.5, p: 1, borderRadius: 1, bgcolor: "action.hover", borderLeft: "3px solid", borderColor: "success.main" }}>
              <Typography variant="caption" color="success.main" sx={{ fontWeight: 600 }}>思考</Typography>
              <Typography variant="body2" sx={{ whiteSpace: "pre-wrap", wordBreak: "break-word", fontSize: 13, mt: 0.25 }}>
                {log.payload.content}
              </Typography>
            </Box>
          )}
          {Object.entries(log.payload).map(([k, v]) => (
            <FieldRow key={k} k={k} v={v} />
          ))}
        </Box>
      )}
    </Box>
  );
}

export default function Chat() {
  const { messages, running, sendMessage, injectMessage, stopTask, debugLogs, clearDebugLogs, processLogs, memoryHint, currentTaskId, fullAccessByTask, setFullAccessForTask } = useTaskStore();
  const [draft, setDraft] = useState("");
  const [logOpen, setLogOpen] = useState(false);
  const [confirmAnchorEl, setConfirmAnchorEl] = useState<HTMLElement | null>(null);
  // 完全访问按 task 记忆：每个 task 各自记住开关，切回时恢复，不串到其他 task
  const fullAccess = fullAccessByTask[currentTaskId] ?? false;
  const endRef = useRef<HTMLDivElement | null>(null);

  // 对话时间线：messages（user/agent/system）+ 实时过程流（thinking/tool_call）
  // 按 ts 归并，过程流自然落在对应 user 消息与最终 agent 总结之间。
  const timeline = useMemo(() => {
    const items: Array<ChatMsg | ProcessItem> = [...messages, ...processLogs];
    items.sort((a, b) => a.ts - b.ts);
    return items;
  }, [messages, processLogs]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, processLogs]);

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
    sendMessage(text, fullAccess);
  };

  return (
    <Box sx={{ display: "flex", height: "100%", width: "100%", minHeight: 0, overflow: "hidden", bgcolor: "action.hover" }}>
      <style>{`@keyframes omni-blink{0%,100%{opacity:1}50%{opacity:0}}`}</style>
      {/* 左：对话区 */}
      <Box
        sx={{
          flexGrow: 1, minWidth: 0, minHeight: 0,
          display: "flex", flexDirection: "column",
          maxWidth: 900, mx: "auto", width: "100%", px: 2, pb: 1.5,
          overflow: "hidden",
        }}
      >
        <Stack direction="row" justifyContent="space-between" alignItems="center"
          sx={{ mb: 1, pt: 1, pb: 1, borderBottom: "1px solid", borderColor: "divider", flexShrink: 0 }}>
          <Typography variant="subtitle1" sx={{ fontWeight: 600 }}>Chat</Typography>
          {/* 预留顶栏右侧控制位（模型切换等） */}
        </Stack>

        {/* K1 记忆更新轻提示（Curator 蒸馏/合并后由 SSE 推送，自动淡出） */}
        {memoryHint && (
          <Alert severity="success" sx={{ mb: 1, flexShrink: 0 }} onClose={() => { /* 由 SSE 计时自动清除 */ }}>
            记忆已更新：蒸馏 {memoryHint.distilled} 条 / 合并 {memoryHint.merged} 条（可在「技能与工具 → 记忆」查看）
          </Alert>
        )}

        <Paper
          elevation={0}
          sx={{ flexGrow: 1, minHeight: 0, mb: 1, bgcolor: "transparent", borderRadius: 2, overflow: "hidden", display: "flex", flexDirection: "column" }}
        >
          <ScrollArea sx={{ flexGrow: 1, minHeight: 0 }}>
            <Box sx={{ p: 2, pr: 1.5 }}>
          {timeline.length === 0 ? (
            <Typography variant="body2" color="text.secondary" sx={{ textAlign: "center", mt: 4 }}>
              开始和 OmniAgent 对话吧。
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
                  node = <MessageBubble item={it} streaming={running && isLast} showRole={groupStart} />;
                else
                  node = <ToolCallCard item={it} />;
              } else if ((it.role === "agent" || it.role === "assistant") && it.extra?.steps?.length) {
                // agent 轮若携带结构化 steps，渲染为「思考 + 工具 + 结论」一体的 agent turn
                node = <AgentTurn m={it} showRole={groupStart} />;
              } else {
                node = <MsgRow m={it} showRole={groupStart} />;
              }

              return (
                <Fragment key={`g${i}`}>
                  {groupStart && curRole && (
                    <Box sx={{ display: "flex", mb: 0.5,
                      justifyContent: curRole === "user" ? "flex-end" : "flex-start" }}>
                      <Chip size="small" label={curRole} color={roleColor(curRole)} />
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

        {/* Codex 风格胶囊输入框：圆角容器 + 内嵌发送/停止按钮 */}
        <Box
          sx={{
            position: "relative",
            border: "1px solid", borderColor: "divider",
            borderRadius: 3,
            bgcolor: "background.paper",
            p: 1,
            flexShrink: 0,
          }}
        >
          <TextField
            placeholder={running ? "运行中可插话（软注入）：输入后回车或点插话按钮，不打断当前步骤" : "给 OmniAgent 发消息…"}
            multiline
            minRows={3}
            maxRows={10}
            fullWidth
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            sx={{
              "& .MuiOutlinedInput-root": {
                borderRadius: 2,
                bgcolor: "transparent",
                "& fieldset": { border: "none" },
                "&:hover fieldset": { border: "none" },
                "&.Mui-focused fieldset": { border: "none" },
              },
              "& .MuiInputLabel-root": { display: "none" },
              "& .MuiOutlinedInput-input": { color: "text.primary", pr: 5 },
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                if (e.ctrlKey || e.metaKey || e.shiftKey) {
                  // 手动插入换行，避免受控组件把默认换行覆盖掉
                  e.preventDefault();
                  const el = e.currentTarget as unknown as HTMLTextAreaElement;
                  const start = el.selectionStart ?? draft.length;
                  const end = el.selectionEnd ?? draft.length;
                  const next = draft.slice(0, start) + "\n" + draft.slice(end);
                  setDraft(next);
                  requestAnimationFrame(() => {
                    el.selectionStart = el.selectionEnd = start + 1;
                  });
                  return;
                }
                e.preventDefault();
                handleSend();
              }
            }}
          />
          {/* 单按钮上下文逻辑：
              - 非运行态：始终「发送」（空输入禁用）
              - 运行态 + 空输入：显示「停止」（红）
              - 运行态 + 有输入：显示「发送」（点即软注入/插话，不打断当前步骤）
              即「用户一输入，暂停变回发送」。 */}
          <IconButton
            onClick={running ? (draft.trim() ? handleInject : stopTask) : handleSend}
            disabled={!running && !draft.trim()}
            aria-label={running && !draft.trim() ? "停止" : "发送"}
            title={running && !draft.trim() ? "停止当前任务" : (running ? "插话（软注入）：不打断当前步骤" : "发送")}
            sx={{
              position: "absolute", right: 10, bottom: 10,
              width: 36, height: 36, flexShrink: 0,
              bgcolor: running && !draft.trim() ? "error.main" : "primary.main",
              color: "#fff",
              "&:hover": { bgcolor: running && !draft.trim() ? "error.dark" : "primary.dark" },
              "&:disabled": { bgcolor: "action.disabledBackground", color: "action.disabled" },
            }}
          >
            {running && !draft.trim() ? <StopIcon /> : <SendIcon />}
          </IconButton>
        </Box>
        {/* 输入框下方 toolbar：无边框、透明，预留模型切换 / skills 选择位 */}
        <Box
          sx={{
            display: "flex",
            flexDirection: "row",
            alignItems: "center",
            gap: 1.5,
            mt: 0.5,
            flexShrink: 0,
            px: 0.5,
          }}
        >
          <FormControlLabel
            control={
              <Switch
                size="small"
                checked={fullAccess}
                disabled={running}
                onChange={(e) => {
                  // 关闭直接生效；开启先在当前位置弹小窗确认风险
                  if (e.target.checked) setConfirmAnchorEl(e.currentTarget);
                  else setFullAccessForTask(currentTaskId, false);
                }}
              />
            }
            label="完全访问"
            sx={{ mr: 0 }}
          />
          {/* 预留 toolbar 位：后续模型切换 / skills 选择放此处 */}
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
          <Typography variant="caption" sx={{ color: "text.secondary", display: "block", mb: 1.5 }}>
            仅对当前任务生效，切换任务后自动关闭。
          </Typography>
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
          点击展开为 420px 窗口面板，后续文件预览 / 可视化等展示窗口复用此格式 */}
      {logOpen ? (
        <Box sx={{ width: 420, flexShrink: 0, borderLeft: "1px solid", borderColor: "divider", display: "flex", flexDirection: "column", minHeight: 0 }}>
          <Stack direction="row" justifyContent="space-between" alignItems="center" sx={{ p: 1, flexShrink: 0 }}>
            <Stack direction="row" spacing={0.5} alignItems="center">
              <BugReportIcon fontSize="small" />
              <Typography variant="subtitle2">Debug</Typography>
            </Stack>
          <Stack direction="row" spacing={0.5} alignItems="center">
            <Button size="small" onClick={clearDebugLogs}>清空</Button>
            <IconButton size="small" onClick={() => setLogOpen(false)} title="收起面板">
              <ChevronRightIcon fontSize="small" />
            </IconButton>
          </Stack>
          </Stack>
          <ScrollArea sx={{ flexGrow: 1, minHeight: 0 }}>
            <Box sx={{ px: 1, pb: 1, pr: 0.5 }}>
            {debugLogs.length === 0 ? (
              <Typography variant="body2" color="text.secondary" sx={{ p: 1 }}>
                暂无 Debug 日志。发送消息后，这里显示：任务下发 / 发给 LLM 的 prompt / 每轮 react（thought + action）。
              </Typography>
            ) : (
              debugLogs.map((l: DebugLog, i: number) => <LogRow key={i} log={l} />)
            )}
            </Box>
          </ScrollArea>
        </Box>
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
