// ── Debug 模块面板（可整体删除）：实时日志 + 记忆查看两个页签 ──────────────
// 删除方式 = 删本文件 + api/debugApi.ts + 右侧栏挂载 + 后端 debug_api.py。
// 实时日志流来自 taskStore 的 debugLogs（SSE `debug` 事件）。
// 2026-10-26 起作为右侧栏（RightDock）的一个页签嵌入，不再自带外框/关闭按钮。
import { useCallback, useEffect, useState } from "react";
import {
  Box,
  Button,
  Chip,
  IconButton,
  ListItemIcon,
  Menu,
  MenuItem,
  Stack,
  Tab,
  Tabs,
  Typography,
} from "@mui/material";
import DeleteOutlinedIcon from "@mui/icons-material/DeleteOutlined";
import ExpandMoreOutlinedIcon from "@mui/icons-material/ExpandMoreOutlined";
import ExpandLessOutlinedIcon from "@mui/icons-material/ExpandLessOutlined";
import RefreshOutlinedIcon from "@mui/icons-material/RefreshOutlined";
import ScrollArea from "./ScrollArea.tsx";
import { debugApi, type DebugMemoryAtom, type DebugMemoryResponse } from "../api/debugApi";
import type { DebugLog } from "../store/taskStore";

function kindColor(kind: string): "default" | "primary" | "secondary" | "error" | "success" | "warning" {
  if (kind === "llm_error") return "error";
  if (kind === "llm_response") return "success";
  if (kind === "llm_request") return "primary";
  if (kind === "tool_result") return "primary";
  if (kind === "task_init") return "secondary";
  if (kind === "skill_catalog" || kind === "skill_call") return "warning";
  if (kind === "memory_flush" || kind === "memory_inject") return "secondary";
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
          {expanded ? <ExpandLessOutlinedIcon /> : <ExpandMoreOutlinedIcon />}
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

function MemoryRow({ atom, onDelete }: { atom: DebugMemoryAtom; onDelete: (atom: DebugMemoryAtom) => void }) {
  const [expanded, setExpanded] = useState(false);
  // 右键删除：菜单锚在鼠标位置，点「删除」直接物理删除（无二次确认，按用户定案）
  const [menuPos, setMenuPos] = useState<{ mouseX: number; mouseY: number } | null>(null);
  const time = atom.updated ? new Date(atom.updated).toLocaleString() : "";
  return (
    <Box
      onContextMenu={(e) => {
        e.preventDefault();
        setMenuPos({ mouseX: e.clientX - 2, mouseY: e.clientY - 4 });
      }}
      sx={{ mb: 1, p: 1, borderRadius: 1, bgcolor: "background.paper" }}
    >
      <Menu
        open={menuPos !== null}
        onClose={() => setMenuPos(null)}
        anchorReference="anchorPosition"
        anchorPosition={menuPos ? { top: menuPos.mouseY, left: menuPos.mouseX } : undefined}
      >
        <MenuItem
          onClick={() => { setMenuPos(null); onDelete(atom); }}
          sx={{ color: "error.main", fontSize: 13 }}
        >
          <ListItemIcon sx={{ minWidth: 28, color: "error.main" }}>
            <DeleteOutlinedIcon fontSize="small" />
          </ListItemIcon>
          删除
        </MenuItem>
      </Menu>
      <Stack direction="row" spacing={0.5} alignItems="center" flexWrap="wrap">
        <Chip size="small" label={atom.kind || "fact"} variant="outlined" />
        {atom.version > 0 && <Chip size="small" label={`v${atom.version}`} variant="outlined" />}
        {atom.source_task && <Chip size="small" label={atom.source_task} variant="outlined" />}
        <Typography variant="caption" color="text.secondary" sx={{ ml: "auto" }}>{time}</Typography>
        <IconButton size="small" sx={{ py: 0 }} onClick={() => setExpanded((v) => !v)}>
          {expanded ? <ExpandLessOutlinedIcon /> : <ExpandMoreOutlinedIcon />}
        </IconButton>
      </Stack>
      <Typography
        variant="body2"
        sx={expanded ? { mt: 0.5, whiteSpace: "pre-wrap", wordBreak: "break-word" } : {
          mt: 0.5,
          display: "-webkit-box",
          WebkitLineClamp: 2,
          WebkitBoxOrient: "vertical",
          overflow: "hidden",
        }}
      >
        {atom.content}
      </Typography>
    </Box>
  );
}

export default function DebugPanel({ logs, onClear, taskId }: {
  logs: DebugLog[];
  onClear: () => void;
  taskId: string;
}) {
  const [tab, setTab] = useState<"log" | "memory">("log");
  const [mem, setMem] = useState<DebugMemoryResponse | null>(null);
  const [loading, setLoading] = useState(false);

  const loadMemory = useCallback(() => {
    setLoading(true);
    debugApi.memory(taskId ? { task_id: taskId } : {})
      .then((r) => setMem(r.data))
      .catch(() => setMem(null))
      .finally(() => setLoading(false));
  }, [taskId]);

  useEffect(() => {
    if (tab !== "memory") return;
    loadMemory();
    // 任务收尾才沉淀记忆；开着记忆页时低频轮询即可看到蒸馏结果落库
    const t = setInterval(loadMemory, 15000);
    return () => clearInterval(t);
  }, [tab, loadMemory]);

  const atoms = mem?.atoms ?? [];
  const count = mem?.count ?? 0;

  // 右键删除单条记忆：成功后重新拉取（计数随刷新对齐）；失败弹提示
  const handleDeleteAtom = useCallback((atom: DebugMemoryAtom) => {
    debugApi.deleteMemory(atom.record_id)
      .then(() => loadMemory())
      .catch((e: unknown) => {
        const err = e as { response?: { data?: { error?: string } }; message?: string };
        window.alert(`删除失败：${err?.response?.data?.error || err?.message || e}`);
      });
  }, [loadMemory]);

  return (
    <Box sx={{ display: "flex", flexDirection: "column", minHeight: 0, height: "100%" }}>
      <Stack direction="row" justifyContent="space-between" alignItems="center"
        sx={{ px: 1, flexShrink: 0, borderBottom: "1px solid", borderColor: "divider" }}>
        <Tabs value={tab} onChange={(_, v) => setTab(v)} sx={{ minHeight: 34 }}>
          <Tab value="log" label="实时日志" sx={{ minHeight: 34, py: 0 }} />
          <Tab value="memory" label="记忆" sx={{ minHeight: 34, py: 0 }} />
        </Tabs>
        {tab === "log" ? (
          <Button size="small" onClick={onClear}>清空</Button>
        ) : (
          <Stack direction="row" spacing={0.5} alignItems="center">
            {loading && <Typography variant="caption" color="text.secondary">加载中…</Typography>}
            <IconButton size="small" onClick={loadMemory} title="刷新">
              <RefreshOutlinedIcon fontSize="small" />
            </IconButton>
          </Stack>
        )}
      </Stack>

      {tab === "log" ? (
        <ScrollArea sx={{ flexGrow: 1, minHeight: 0 }}>
          <Box sx={{ px: 1, pb: 1, pr: 0.5 }}>
            {logs.length === 0 ? (
              <Typography variant="body2" color="text.secondary" sx={{ p: 1 }}>
                暂无 Debug 日志。发送消息后，这里显示：任务下发 / LLM 请求与响应 / 每步工具结果 / skill 调用与目录注入 / 记忆注入与沉淀。
              </Typography>
            ) : (
              logs.map((l: DebugLog, i: number) => <LogRow key={i} log={l} />)
            )}
          </Box>
        </ScrollArea>
      ) : (
        <Box sx={{ flexGrow: 1, minHeight: 0, display: "flex", flexDirection: "column" }}>
          <Stack direction="row" spacing={1} alignItems="center" sx={{ px: 1, py: 0.5, flexShrink: 0 }}>
            <Typography variant="caption" color="text.secondary">
              {mem ? `记忆库 · ${count} 条` : (loading ? "加载中…" : "")}
            </Typography>
            {mem?.overview ? (
              <Typography variant="caption" color="text.secondary">
                {Object.entries(mem.overview.by_state)
                  .map(([k, v]) => `${k} ${v}`)
                  .join(" / ")}
              </Typography>
            ) : null}
          </Stack>
          <ScrollArea sx={{ flexGrow: 1, minHeight: 0 }}>
            <Box sx={{ px: 1, pb: 1, pr: 0.5 }}>
              {atoms.length === 0 ? (
                <Typography variant="body2" color="text.secondary" sx={{ p: 1 }}>
                  {mem ? "暂无记忆 atoms——任务收尾蒸馏后落库（需 ≥1 条事实被提炼）。" : (loading ? "加载中…" : "加载失败")}
                </Typography>
              ) : (
                atoms.map((a) => <MemoryRow key={a.record_id} atom={a} onDelete={handleDeleteAtom} />)
              )}
            </Box>
          </ScrollArea>
        </Box>
      )}
    </Box>
  );
}
