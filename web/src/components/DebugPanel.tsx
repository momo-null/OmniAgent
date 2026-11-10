// ── Debug 侧边栏：左侧 activity rail + 右侧工具窗口（Codex / IDEA 插件风格） ──
// 删除方式 = 删本文件 + api/debugApi.ts + components/debug/ + 右侧栏挂载 +
// 后端 debug_api.py、debug_memory_data.py 与 server.py 挂载行。
import { useState } from "react";
import { Box, IconButton, Stack, Tooltip, Typography } from "@mui/material";
import {
  EraserIcon, TerminalIcon, ZapIcon,
} from "./icons";
import ScrollArea from "./ScrollArea.tsx";
import MetricsPanel from "./debug/MetricsPanel.tsx";
import { Empty, KV, Pre, Row, StatusBar, Toolbar } from "./debug/shared";
import type { DebugLog } from "../store/taskStore";

type View = "log" | "metrics";
const VIEWS: { key: View; label: string; icon: typeof TerminalIcon }[] = [
  { key: "log", label: "实时日志", icon: TerminalIcon },
  { key: "metrics", label: "监控", icon: ZapIcon },
];

function dotColor(kind: string): string {
  if (kind === "llm_error") return "error.main";
  if (kind === "llm_response") return "success.main";
  if (kind === "llm_request" || kind === "tool_result") return "primary.main";
  if (kind === "memory_flush" || kind === "memory_inject") return "secondary.main";
  if (kind === "skill_catalog" || kind === "skill_call") return "warning.main";
  return "text.disabled";
}

function LogRow({ log }: { log: DebugLog }) {
  const [open, setOpen] = useState(false);
  const short: Record<string, unknown> = {};
  const longs: [string, string][] = [];
  for (const [k, v] of Object.entries(log.payload || {})) {
    if (typeof v === "string" && v.length > 200) longs.push([k, v]);
    else short[k] = v;
  }
  return (
    <Box>
      <Stack direction="row" alignItems="center" spacing={0.75}
        onClick={() => setOpen((v) => !v)}
        sx={{
          pl: 1, pr: 1, minHeight: 23, cursor: "pointer",
          borderLeft: "2px solid", borderColor: open ? "primary.main" : "transparent",
          bgcolor: open ? "action.selected" : "transparent",
          "&:hover": { bgcolor: "action.hover" },
        }}>
        <Box sx={{ width: 5, height: 5, borderRadius: "50%", bgcolor: dotColor(log.kind), flexShrink: 0 }} />
        <Typography noWrap sx={{ fontSize: 10.5, color: "text.disabled", width: 88, flexShrink: 0 }}>
          {log.kind}
        </Typography>
        <Typography noWrap sx={{ fontSize: 11.5, flex: 1, minWidth: 0 }}>{log.title}</Typography>
        <Typography sx={{ fontSize: 10, color: "text.disabled", flexShrink: 0 }}>
          {new Date(log.ts).toLocaleTimeString()}
        </Typography>
      </Stack>
      {open && (
        <Box>
          <KV data={short} />
          {longs.map(([k, v]) => (
            <Box key={k}>
              <Row label={k} value={`${v.length} 字符`} />
              <Pre text={v} maxHeight={200} />
            </Box>
          ))}
        </Box>
      )}
    </Box>
  );
}

function LogPane({ logs }: { logs: DebugLog[] }) {
  return (
    <Box sx={{ display: "flex", flexDirection: "column", height: "100%", minHeight: 0 }}>
      <Toolbar title="console" extra={
        <Typography sx={{ fontSize: 10, color: "text.disabled" }}>{logs.length} 条</Typography>
      } />
      <ScrollArea sx={{ flexGrow: 1, minHeight: 0 }}>
        <Box sx={{ py: 0.4 }}>
          {logs.length === 0 ? (
            <Empty text="暂无日志。发送消息后显示任务下发 / LLM 请求响应 / 工具结果 / 记忆注入与沉淀。" />
          ) : logs.map((l, i) => <LogRow key={i} log={l} />)}
        </Box>
      </ScrollArea>
      <StatusBar>
        <Typography sx={{ fontSize: 10, color: "text.disabled" }}>SSE debug 事件 · 仅本机</Typography>
      </StatusBar>
    </Box>
  );
}

export default function DebugPanel({ logs, onClear }: {
  logs: DebugLog[];
  onClear: () => void;
}) {
  const [view, setView] = useState<View>("log");

  return (
    <Stack direction="row" sx={{ height: "100%", minHeight: 0 }}>
      {/* activity rail：36px 竖条，选中项左侧主色指示条 */}
      <Stack alignItems="center" spacing={0.4}
        sx={{ width: 36, flexShrink: 0, py: 0.5, borderRight: "1px solid", borderColor: "divider" }}>
        {VIEWS.map((v) => {
          const Icon = v.icon;
          const active = view === v.key;
          return (
            <Tooltip key={v.key} title={v.label} placement="right">
              <IconButton size="small" onClick={() => setView(v.key)}
                sx={{
                  width: 30, height: 30, p: 0.5, borderRadius: 0.5,
                  borderLeft: "2px solid", borderColor: active ? "primary.main" : "transparent",
                  color: active ? "primary.main" : "text.disabled",
                  bgcolor: active ? "action.selected" : "transparent",
                  "&:hover": { color: "text.primary", bgcolor: "action.hover" },
                }}>
                <Icon sx={{ fontSize: 16 }} />
              </IconButton>
            </Tooltip>
          );
        })}
        <Box sx={{ flex: 1 }} />
        {view === "log" && (
          <Tooltip title="清空日志" placement="right">
            <IconButton size="small" onClick={onClear} sx={{ width: 30, height: 30, p: 0.5, color: "text.disabled" }}>
              <EraserIcon sx={{ fontSize: 15 }} />
            </IconButton>
          </Tooltip>
        )}
      </Stack>

      <Box sx={{ flex: 1, minWidth: 0, minHeight: 0 }}>
        {view === "log" ? <LogPane logs={logs} /> : <MetricsPanel />}
      </Box>
    </Stack>
  );
}
