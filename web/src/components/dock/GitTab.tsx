// 右侧栏「git 变更」页：任意仓库路径的分支 + 变更清单 + 单文件 diff。
// 路径由用户自填（缺省 = 后端进程工作目录），持久化到 localStorage。
import { useCallback, useEffect, useRef, useState } from "react";
import {
  Box,
  Button,
  Chip,
  CircularProgress,
  IconButton,
  InputBase,
  Stack,
  Typography,
} from "@mui/material";
import ArrowBackOutlined from "@mui/icons-material/ArrowBackOutlined";
import RefreshOutlined from "@mui/icons-material/RefreshOutlined";
import ScrollArea from "../ScrollArea.tsx";
import { workspaceApi } from "../../api/client";
import type { GitDiffResp, GitStatusResp } from "../../types";

const PATH_KEY = "omni.dock.gitPath";

// diff 行着色：+ 绿 / - 红 / @@ 高亮，其余默认
function DiffLine({ line }: { line: string }) {
  const color = line.startsWith("+") && !line.startsWith("+++")
    ? "success.main"
    : line.startsWith("-") && !line.startsWith("---")
      ? "error.main"
      : line.startsWith("@@")
        ? "info.main"
        : "text.primary";
  return (
    <Box component="span" sx={{ display: "block", color, whiteSpace: "pre-wrap", wordBreak: "break-all" }}>
      {line || " "}
    </Box>
  );
}

function statusColor(label: string): "default" | "primary" | "secondary" | "error" | "success" | "warning" {
  if (label === "新增" || label === "未跟踪") return "success";
  if (label === "修改") return "warning";
  if (label === "删除") return "error";
  if (label === "重命名") return "primary";
  if (label === "冲突") return "error";
  return "default";
}

export default function GitTab() {
  const [path, setPath] = useState(() => localStorage.getItem(PATH_KEY) || "");
  const [status, setStatus] = useState<GitStatusResp | null>(null);
  const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState("");
  const [diff, setDiff] = useState<GitDiffResp | null>(null);
  const [diffLoading, setDiffLoading] = useState(false);
  const pathInputRef = useRef("");

  const loadStatus = useCallback((p: string) => {
    setLoading(true);
    workspaceApi.gitStatus(p)
      .then((r) => setStatus(r.data))
      .catch((e) => setStatus({ ok: false, is_repo: false, error: e?.message || "加载失败" }))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    setSelected("");
    setDiff(null);
    loadStatus(path);
  }, [path, loadStatus]);

  const applyPath = () => {
    const p = pathInputRef.current || path;
    localStorage.setItem(PATH_KEY, p);
    setPath(p);
  };

  const openDiff = (file: string) => {
    setSelected(file);
    setDiffLoading(true);
    workspaceApi.gitDiff(path, file)
      .then((r) => setDiff(r.data))
      .catch((e) => setDiff({ ok: false, file, untracked: false, diff: "", error: e?.message || "加载失败" }))
      .finally(() => setDiffLoading(false));
  };

  // ── diff 视图 ──
  if (selected) {
    const lines = (diff?.diff || "").split("\n");
    return (
      <Box sx={{ display: "flex", flexDirection: "column", minHeight: 0, height: "100%" }}>
        <Stack direction="row" spacing={0.5} alignItems="center"
          sx={{ px: 1, py: 0.5, flexShrink: 0, borderBottom: "1px solid", borderColor: "divider" }}>
          <IconButton size="small" onClick={() => { setSelected(""); setDiff(null); }} title="返回变更清单">
            <ArrowBackOutlined fontSize="small" />
          </IconButton>
          <Typography variant="body2" noWrap sx={{ flex: 1, minWidth: 0, fontWeight: 600, fontSize: 13 }}>
            {selected}
          </Typography>
          {diff?.untracked && <Chip size="small" label="新文件" color="success" variant="outlined" sx={{ height: 18, fontSize: 11 }} />}
        </Stack>
        <ScrollArea sx={{ flexGrow: 1, minHeight: 0 }}>
          <Box sx={{ p: 1 }}>
            {diffLoading ? (
              <Box sx={{ display: "flex", justifyContent: "center", py: 3 }}><CircularProgress size={20} /></Box>
            ) : diff?.ok === false ? (
              <Typography variant="body2" color="error">{diff.error || "加载失败"}</Typography>
            ) : (
              <Box component="pre" sx={{
                m: 0, p: 1, borderRadius: 1, bgcolor: "action.hover",
                fontFamily: "monospace", fontSize: 12, lineHeight: 1.5, minHeight: 40,
              }}>
                {lines.map((ln, i) => <DiffLine key={i} line={ln} />)}
              </Box>
            )}
          </Box>
        </ScrollArea>
      </Box>
    );
  }

  // ── 变更清单视图 ──
  const entries = status?.entries || [];
  return (
    <Box sx={{ display: "flex", flexDirection: "column", minHeight: 0, height: "100%" }}>
      <Stack direction="row" spacing={0.5} alignItems="center"
        sx={{ px: 1, py: 0.75, flexShrink: 0, borderBottom: "1px solid", borderColor: "divider" }}>
        <InputBase
          defaultValue={path}
          key={path}
          inputRef={pathInputRef}
          onKeyDown={(e) => { if (e.key === "Enter") applyPath(); }}
          onBlur={applyPath}
          placeholder="仓库路径（如 D:\AI\OmniAgent）"
          size="small"
          sx={{ flex: 1, minWidth: 0, fontSize: 12.5, px: 1, bgcolor: "action.hover", borderRadius: 1, height: 30 }}
        />
        {loading && <CircularProgress size={14} />}
        <IconButton size="small" title="刷新" onClick={() => loadStatus(path)}>
          <RefreshOutlined fontSize="small" />
        </IconButton>
      </Stack>
      {status?.is_repo && (
        <Stack direction="row" spacing={1} alignItems="center" sx={{ px: 1.5, py: 0.5, flexShrink: 0 }}>
          <Chip size="small" label={status.branch || "HEAD"} variant="outlined" sx={{ height: 18, fontSize: 11 }} />
          <Typography variant="caption" color="text.secondary" noWrap sx={{ fontSize: 11 }}>
            {entries.length > 0 ? `${entries.length} 个变更` : "工作区干净"}
          </Typography>
        </Stack>
      )}
      <ScrollArea sx={{ flexGrow: 1, minHeight: 0 }}>
        <Box sx={{ px: 1, py: 0.5 }}>
          {status?.ok === false ? (
            <Typography variant="body2" color="error" sx={{ p: 1 }}>{status.error}</Typography>
          ) : status && !status.is_repo ? (
            <Typography variant="body2" color="text.secondary" sx={{ p: 1 }}>
              该目录不是 git 仓库。可在上方输入其它仓库路径。
            </Typography>
          ) : entries.length === 0 ? (
            <Typography variant="body2" color="text.secondary" sx={{ p: 1 }}>
              {loading ? "加载中…" : "无变更。"}
            </Typography>
          ) : (
            entries.map((e) => (
              <Box
                key={`${e.x}${e.y}${e.file}`}
                onClick={() => openDiff(e.file)}
                sx={{
                  display: "flex", alignItems: "center", gap: 1, px: 1, py: 0.6,
                  borderRadius: 1, cursor: "pointer", "&:hover": { bgcolor: "action.hover" },
                }}
              >
                <Chip size="small" label={e.label} color={statusColor(e.label)} variant="outlined"
                  sx={{ height: 17, fontSize: 10.5, flexShrink: 0, "& .MuiChip-label": { px: 0.6 } }} />
                <Typography variant="body2" noWrap sx={{ flex: 1, minWidth: 0, fontSize: 12.5, fontFamily: "monospace" }}>
                  {e.file}
                </Typography>
              </Box>
            ))
          )}
        </Box>
      </ScrollArea>
      {!path && (
        <Box sx={{ px: 2, py: 1, flexShrink: 0 }}>
          <Button size="small" fullWidth variant="outlined"
            onClick={() => { localStorage.setItem(PATH_KEY, ""); setPath(""); }}>
            使用后端默认工作目录
          </Button>
        </Box>
      )}
    </Box>
  );
}
