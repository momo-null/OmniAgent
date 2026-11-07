// ── 桌面侧边栏风格共用件：可折叠分组 / 单行条目 / 紧凑键值（无卡片、无网格） ──
// 对齐 Codex / IDEA 插件工具窗口：低密度行、分组头、hover 高亮、底部状态条。
import { useState, type ReactNode } from "react";
import { Box, IconButton, Stack, Typography } from "@mui/material";
import { ChevronDownIcon, ChevronRightIcon, RefreshCwIcon } from "../icons";

export function Toolbar({ title, onRefresh, loading, extra }: {
  title: string; onRefresh?: () => void; loading?: boolean; extra?: ReactNode;
}) {
  return (
    <Stack direction="row" alignItems="center" spacing={0.5}
      sx={{ px: 1, minHeight: 30, flexShrink: 0, borderBottom: "1px solid", borderColor: "divider" }}>
      <Typography sx={{ fontSize: 10.5, fontWeight: 700, letterSpacing: 1, color: "text.secondary", flex: 1 }}>
        {title.toUpperCase()}
      </Typography>
      {loading && <Typography sx={{ fontSize: 10, color: "text.disabled" }}>刷新中</Typography>}
      {extra}
      {onRefresh && (
        <IconButton size="small" onClick={onRefresh} sx={{ p: 0.25 }} title="刷新">
          <RefreshCwIcon sx={{ fontSize: 13 }} />
        </IconButton>
      )}
    </Stack>
  );
}

export function StatusBar({ children }: { children: ReactNode }) {
  return (
    <Stack direction="row" alignItems="center" spacing={0.75}
      sx={{ px: 1, minHeight: 22, flexShrink: 0, borderTop: "1px solid", borderColor: "divider" }}>
      {children}
    </Stack>
  );
}

export function Section({ title, count, defaultOpen = false, children }: {
  title: string; count?: ReactNode; defaultOpen?: boolean; children: ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <Box>
      <Stack direction="row" alignItems="center" spacing={0.3}
        onClick={() => setOpen((v) => !v)}
        sx={{
          px: 0.75, minHeight: 25, cursor: "pointer", userSelect: "none",
          bgcolor: "action.hover", borderTop: "1px solid", borderColor: "divider",
          "&:hover": { color: "primary.main" },
        }}>
        {open ? <ChevronDownIcon sx={{ fontSize: 12 }} /> : <ChevronRightIcon sx={{ fontSize: 12 }} />}
        <Typography sx={{ fontSize: 10, fontWeight: 700, letterSpacing: 0.9, color: "text.secondary", flex: 1, minWidth: 0 }} noWrap>
          {title.toUpperCase()}
        </Typography>
        {count !== undefined && (
          <Typography sx={{ fontSize: 10, color: "text.disabled", flexShrink: 0 }}>{count}</Typography>
        )}
      </Stack>
      {open && <Box sx={{ py: 0.25 }}>{children}</Box>}
    </Box>
  );
}

export function GroupLabel({ children }: { children: ReactNode }) {
  return (
    <Typography sx={{ px: 1.5, pt: 0.6, pb: 0.2, fontSize: 10, color: "text.disabled" }}>
      {children}
    </Typography>
  );
}

export function Row({ label, value, onClick, selected, tone, title, indent = 0 }: {
  label: ReactNode; value?: ReactNode; onClick?: () => void; selected?: boolean;
  tone?: "ok" | "warn" | "error"; title?: string; indent?: number;
}) {
  const color = tone === "error" ? "error.main" : tone === "warn" ? "warning.main"
    : tone === "ok" ? "success.main" : undefined;
  return (
    <Stack direction="row" alignItems="center" spacing={0.75} onClick={onClick} title={title}
      sx={{
        pl: 1 + indent * 1.25, pr: 1, minHeight: 23,
        cursor: onClick ? "pointer" : "default",
        borderLeft: "2px solid", borderColor: selected ? "primary.main" : "transparent",
        bgcolor: selected ? "action.selected" : "transparent",
        "&:hover": onClick ? { bgcolor: "action.hover" } : {},
      }}>
      <Typography noWrap sx={{ fontSize: 11.5, flex: 1, minWidth: 0, color }}>{label}</Typography>
      {value !== undefined && (
        <Typography noWrap sx={{ fontSize: 10.5, color: color || "text.disabled", flexShrink: 0, maxWidth: "52%", textAlign: "right" }}>
          {value}
        </Typography>
      )}
    </Stack>
  );
}

export function TinyBar({ value, max, color }: { value: number; max: number; color?: string }) {
  const w = max > 0 ? Math.max(2, Math.min(100, (value / max) * 100)) : 0;
  return (
    <Box sx={{ mx: 1.25, mb: 0.35, height: 3, bgcolor: "action.hover" }}>
      <Box sx={{ height: "100%", width: `${w}%`, bgcolor: color || "primary.main" }} />
    </Box>
  );
}

function fmt(v: unknown): string {
  if (v === null || v === undefined || v === "") return "—";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

export function KV({ data, skip = [] }: { data: Record<string, unknown>; skip?: string[] }) {
  const entries = Object.entries(data || {}).filter(([k]) => !skip.includes(k));
  if (!entries.length) return <Empty text="无数据" />;
  return (
    <Box sx={{ px: 1.25, py: 0.3, bgcolor: "action.hover" }}>
      {entries.map(([k, v]) => (
        <Stack key={k} direction="row" spacing={0.75} sx={{ py: 0.1 }}>
          <Typography sx={{ fontSize: 10.5, color: "text.secondary", minWidth: 88, flexShrink: 0 }} noWrap>{k}</Typography>
          <Typography sx={{ fontSize: 10.5, wordBreak: "break-all", flex: 1, minWidth: 0 }}>{fmt(v)}</Typography>
        </Stack>
      ))}
    </Box>
  );
}

export function Pre({ text, maxHeight = 260 }: { text: string; maxHeight?: number }) {
  return (
    <Box component="pre" sx={{
      m: 0, px: 1.25, py: 0.75, bgcolor: "action.hover", fontSize: 11,
      whiteSpace: "pre-wrap", wordBreak: "break-word", maxHeight, overflow: "auto",
      borderLeft: "2px solid", borderColor: "divider",
    }}>
      {text}
    </Box>
  );
}

export function Empty({ text }: { text: string }) {
  return (
    <Typography sx={{ px: 1.5, py: 0.8, fontSize: 11, color: "text.disabled" }}>{text}</Typography>
  );
}

export function num(v: unknown, digits = 2): string {
  if (typeof v !== "number" || !Number.isFinite(v)) return "—";
  return digits === 0 ? String(Math.round(v)) : v.toFixed(digits);
}

export function pct(v: unknown): string {
  if (typeof v !== "number" || !Number.isFinite(v)) return "—";
  return `${(v * 100).toFixed(1)}%`;
}
