// 右侧栏「终端」页：命令执行回显（每次独立子进程，带墙钟超时；非交互 PTY）。
// 会话 cwd 在多次执行间保持（后端回传实际 cwd），命令历史支持 ↑/↓。
import { useCallback, useEffect, useRef, useState } from "react";
import { Box, IconButton, InputBase, Stack, Tooltip, Typography } from "@mui/material";
import StopCircleRounded from "@mui/icons-material/StopCircleRounded";
import PlayArrowRounded from "@mui/icons-material/PlayArrowRounded";
import DeleteSweepRounded from "@mui/icons-material/DeleteSweepRounded";
import ScrollArea from "../ScrollArea.tsx";
import { workspaceApi } from "../../api/client";
import type { TerminalResp } from "../../types";

type Line = { kind: "in" | "out" | "err" | "sys"; text: string };

export default function TerminalTab() {
  const [lines, setLines] = useState<Line[]>([]);
  const [cmd, setCmd] = useState("");
  const [cwd, setCwd] = useState(""); // 空 = 后端默认（首次执行后由响应回填）
  const [busy, setBusy] = useState(false);
  const historyRef = useRef<string[]>([]);
  const histIdxRef = useRef(-1);
  const viewportRef = useRef<HTMLDivElement | null>(null);

  // 新输出自动贴底（用户上翻时暂停跟随）
  const pinnedRef = useRef(true);
  useEffect(() => {
    const el = viewportRef.current;
    if (!el) return;
    const onScroll = () => {
      pinnedRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    };
    el.addEventListener("scroll", onScroll);
    return () => el.removeEventListener("scroll", onScroll);
  }, []);
  useEffect(() => {
    const el = viewportRef.current;
    if (el && pinnedRef.current) el.scrollTop = el.scrollHeight;
  }, [lines]);

  const push = useCallback((l: Line) => setLines((prev) => [...prev.slice(-500), l]), []);

  const run = async () => {
    const command = cmd.trim();
    if (!command || busy) return;
    setCmd("");
    histIdxRef.current = -1;
    historyRef.current = [...historyRef.current.slice(-100), command];
    push({ kind: "in", text: command });
    setBusy(true);
    try {
      const r = await workspaceApi.terminal({ command, cwd: cwd || undefined });
      const d = r.data as TerminalResp;
      if (d.cwd) setCwd(d.cwd);
      const out = (d.output || "").trim();
      if (out) push({ kind: d.ok ? "out" : "err", text: out });
      if (!out) push({ kind: "sys", text: d.ok ? "（无输出）" : `执行失败（returncode=${d.returncode ?? "?"}）` });
    } catch (e: unknown) {
      const err = e as { response?: { data?: { error?: string } }; message?: string };
      push({ kind: "err", text: err?.response?.data?.error || err?.message || "请求失败" });
    } finally {
      setBusy(false);
    }
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter") {
      e.preventDefault();
      void run();
      return;
    }
    // ↑/↓ 翻命令历史
    const hist = historyRef.current;
    if (e.key === "ArrowUp" && hist.length > 0) {
      e.preventDefault();
      histIdxRef.current = histIdxRef.current < 0 ? hist.length - 1 : Math.max(0, histIdxRef.current - 1);
      setCmd(hist[histIdxRef.current]);
    } else if (e.key === "ArrowDown" && histIdxRef.current >= 0) {
      e.preventDefault();
      histIdxRef.current += 1;
      if (histIdxRef.current >= hist.length) {
        histIdxRef.current = -1;
        setCmd("");
      } else {
        setCmd(hist[histIdxRef.current]);
      }
    }
  };

  const colorOf = (k: Line["kind"]) =>
    k === "in" ? "primary.main" : k === "err" ? "error.main" : k === "sys" ? "text.disabled" : "text.primary";

  return (
    <Box sx={{ display: "flex", flexDirection: "column", minHeight: 0, height: "100%" }}>
      <Stack direction="row" spacing={0.5} alignItems="center"
        sx={{ px: 1, py: 0.5, flexShrink: 0, borderBottom: "1px solid", borderColor: "divider" }}>
        <Tooltip title={cwd || "缺省：后端工作目录"}>
          <Typography variant="caption" color="text.secondary" noWrap sx={{ flex: 1, minWidth: 0, fontSize: 11, fontFamily: "monospace" }}>
            {cwd || "（缺省工作目录）"}
          </Typography>
        </Tooltip>
        <IconButton size="small" title="清屏" onClick={() => setLines([])}>
          <DeleteSweepRounded fontSize="small" />
        </IconButton>
      </Stack>
      <ScrollArea sx={{ flexGrow: 1, minHeight: 0 }} viewportRef={viewportRef}>
        <Box sx={{ px: 1, py: 0.5, minHeight: 60 }}>
          {lines.length === 0 ? (
            <Typography variant="body2" color="text.secondary" sx={{ p: 1 }}>
              在这里执行命令（如 git status、python script.py）。每次独立子进程，超时 60 秒；不支持交互式程序。
            </Typography>
          ) : (
            lines.map((l, i) => (
              <Box key={i} component="pre" sx={{
                m: 0, py: 0.1, fontFamily: "monospace", fontSize: 12, lineHeight: 1.5,
                color: colorOf(l.kind), whiteSpace: "pre-wrap", wordBreak: "break-word",
                display: "block",
              }}>
                {l.kind === "in" ? `❯ ${l.text}` : l.text}
              </Box>
            ))
          )}
          {busy && <Typography variant="caption" color="text.secondary" sx={{ px: 1 }}>执行中…</Typography>}
        </Box>
      </ScrollArea>
      <Stack direction="row" spacing={0.5} alignItems="center"
        sx={{ px: 1, py: 0.75, flexShrink: 0, borderTop: "1px solid", borderColor: "divider" }}>
        <Typography variant="body2" sx={{ fontFamily: "monospace", color: "primary.main", flexShrink: 0 }}>❯</Typography>
        <InputBase
          value={cmd}
          onChange={(e) => setCmd(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder="输入命令，回车执行"
          disabled={busy}
          size="small"
          sx={{ flex: 1, minWidth: 0, fontSize: 13, px: 1, bgcolor: "action.hover", borderRadius: 1, height: 30, fontFamily: "monospace" }}
        />
        <IconButton size="small" title={busy ? "执行中" : "执行"} disabled={busy || !cmd.trim()} onClick={() => void run()}>
          {busy ? <StopCircleRounded fontSize="small" /> : <PlayArrowRounded fontSize="small" />}
        </IconButton>
      </Stack>
    </Box>
  );
}
