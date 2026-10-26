// 右侧栏「文件」页：任务产物列表 + 文件预览（markdown 渲染 / 文本 / 图片）。
// 数据源 = 后端产物登记表（GET /api/runtime/artifacts），与结果卡「产物」同源：
// agent 通过 write_file / edit_file / 截图等工具实际写出的文件才会登记。
import { useCallback, useEffect, useRef, useState } from "react";
import {
  Box,
  Button,
  Chip,
  CircularProgress,
  IconButton,
  Stack,
  Tooltip,
  Typography,
} from "@mui/material";
import ArrowBackRounded from "@mui/icons-material/ArrowBackRounded";
import DescriptionRounded from "@mui/icons-material/DescriptionRounded";
import ImageIcon from "@mui/icons-material/ImageRounded";
import InsertDriveFileRounded from "@mui/icons-material/InsertDriveFileRounded";
import OpenInNewRounded from "@mui/icons-material/OpenInNewRounded";
import DownloadRounded from "@mui/icons-material/DownloadRounded";
import RefreshRounded from "@mui/icons-material/RefreshRounded";
import ScrollArea from "../ScrollArea.tsx";
import Markdown from "../Markdown.tsx";
import { workspaceApi } from "../../api/client";
import { useTaskStore } from "../../store/taskStore.tsx";
import type { ArtifactInfo, FilePreview } from "../../types";

const IMAGE_EXTS = new Set([".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".svg"]);
const MD_EXTS = new Set([".md", ".markdown"]);

export function fmtBytes(n?: number): string {
  if (n == null || Number.isNaN(n)) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function extOf(name: string): string {
  const i = name.lastIndexOf(".");
  return i >= 0 ? name.slice(i).toLowerCase() : "";
}

function ArtifactIcon({ name }: { name: string }) {
  const ext = extOf(name);
  if (IMAGE_EXTS.has(ext)) return <ImageIcon fontSize="small" sx={{ color: "#42a5f5" }} />;
  if (MD_EXTS.has(ext)) return <DescriptionRounded fontSize="small" sx={{ color: "#26c6da" }} />;
  return <InsertDriveFileRounded fontSize="small" sx={{ color: "text.disabled" }} />;
}

function PreviewBody({ taskId, p }: { taskId: string; p: FilePreview }) {
  const ext = extOf(p.name);
  if (p.kind === "image") {
    return (
      <Box sx={{ p: 1, display: "flex", justifyContent: "center" }}>
        <Box
          component="img"
          src={workspaceApi.fileRawUrl(taskId, p.path)}
          alt={p.name}
          sx={{ maxWidth: "100%", maxHeight: "60vh", borderRadius: 1 }}
        />
      </Box>
    );
  }
  if (p.kind === "binary") {
    return (
      <Box sx={{ p: 2 }}>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
          二进制文件（{fmtBytes(p.size)}），暂不支持预览。
        </Typography>
        <Button size="small" startIcon={<DownloadRounded />}
          href={workspaceApi.fileRawUrl(taskId, p.path, true)}>
          下载文件
        </Button>
      </Box>
    );
  }
  if (MD_EXTS.has(ext)) {
    return (
      <Box sx={{ px: 1.5, py: 1 }}>
        <Markdown>{p.content || ""}</Markdown>
      </Box>
    );
  }
  return (
    <Box sx={{ px: 1, py: 1 }}>
      <Box
        component="pre"
        sx={{
          m: 0, p: 1, borderRadius: 1, bgcolor: "action.hover",
          fontFamily: "monospace", fontSize: 12, lineHeight: 1.5,
          whiteSpace: "pre-wrap", wordBreak: "break-word",
        }}
      >
        {p.content || ""}
        {p.truncated ? "\n...[内容过长已截断]" : ""}
      </Box>
    </Box>
  );
}

export default function FilesTab() {
  const {
    currentTaskId, running, dockPreviewPath, setDockPreviewPath,
    openDock, artifactsVersion, bumpArtifactsVersion,
  } = useTaskStore();
  const [arts, setArts] = useState<ArtifactInfo[]>([]);
  const [preview, setPreview] = useState<FilePreview | null>(null);
  const [loading, setLoading] = useState(false);
  const [previewLoading, setPreviewLoading] = useState(false);
  // 记录正在预览的路径：dockPreviewPath 与本地预览一致时避免重复请求
  const previewPathRef = useRef("");

  const loadArtifacts = useCallback(() => {
    if (!currentTaskId) { setArts([]); return; }
    setLoading(true);
    workspaceApi.artifacts(currentTaskId)
      .then((r) => setArts((r.data as { artifacts?: ArtifactInfo[] })?.artifacts || []))
      .catch(() => setArts([]))
      .finally(() => setLoading(false));
  }, [currentTaskId]);

  // 任务切换：清空预览态（登记表由下方 effect 拉取）
  useEffect(() => {
    setPreview(null);
    previewPathRef.current = "";
    setDockPreviewPath("");
  }, [currentTaskId, setDockPreviewPath]);

  // 任务切换 / 结果卡「查看所有产物」触发 → 拉取登记表（不碰预览态，避免打断刚点开的预览）
  useEffect(() => {
    loadArtifacts();
  }, [currentTaskId, artifactsVersion, loadArtifacts]);

  // 运行中低频轮询：agent 刚写出的产物实时出现在列表里
  useEffect(() => {
    if (!running || !currentTaskId) return;
    const t = setInterval(loadArtifacts, 3000);
    return () => clearInterval(t);
  }, [running, currentTaskId, loadArtifacts]);

  // dockPreviewPath（结果卡点击 / 列表点击统一入口）→ 拉取预览
  useEffect(() => {
    if (!dockPreviewPath || !currentTaskId) return;
    if (previewPathRef.current === dockPreviewPath) return;
    previewPathRef.current = dockPreviewPath;
    setPreviewLoading(true);
    workspaceApi.readFile(currentTaskId, dockPreviewPath)
      .then((r) => setPreview(r.data as FilePreview))
      .catch(() => setPreview({
        ok: false, path: dockPreviewPath, name: dockPreviewPath.split(/[\\/]/).pop() || "",
        kind: "text", error: "预览加载失败",
      }))
      .finally(() => setPreviewLoading(false));
  }, [dockPreviewPath, currentTaskId]);

  const backToList = () => {
    previewPathRef.current = "";
    setPreview(null);
    setDockPreviewPath("");
  };

  // ── 预览态 ──
  if (dockPreviewPath) {
    return (
      <Box sx={{ display: "flex", flexDirection: "column", minHeight: 0, height: "100%" }}>
        <Stack direction="row" spacing={0.5} alignItems="center"
          sx={{ px: 1, py: 0.5, flexShrink: 0, borderBottom: "1px solid", borderColor: "divider" }}>
          <IconButton size="small" onClick={backToList} title="返回产物列表">
            <ArrowBackRounded fontSize="small" />
          </IconButton>
          {preview && <ArtifactIcon name={preview.name} />}
          <Tooltip title={preview?.path || dockPreviewPath}>
            <Typography variant="body2" noWrap sx={{ flex: 1, minWidth: 0, fontWeight: 600, fontSize: 13 }}>
              {preview?.name || dockPreviewPath.split(/[\\/]/).pop()}
            </Typography>
          </Tooltip>
          {preview?.size != null && (
            <Typography variant="caption" color="text.secondary" sx={{ flexShrink: 0 }}>
              {fmtBytes(preview.size)}
            </Typography>
          )}
          <Tooltip title="在系统浏览器打开">
            <IconButton size="small" title="在系统浏览器打开"
              onClick={() => currentTaskId && window.open(
                workspaceApi.fileRawUrl(currentTaskId, dockPreviewPath), "_blank")}>
              <OpenInNewRounded fontSize="small" />
            </IconButton>
          </Tooltip>
        </Stack>
        <ScrollArea sx={{ flexGrow: 1, minHeight: 0 }}>
          {previewLoading ? (
            <Box sx={{ display: "flex", justifyContent: "center", py: 4 }}>
              <CircularProgress size={22} />
            </Box>
          ) : preview?.ok === false ? (
            <Typography variant="body2" color="error" sx={{ p: 2 }}>
              {preview.error || "预览失败"}
            </Typography>
          ) : preview ? (
            <PreviewBody taskId={currentTaskId} p={preview} />
          ) : null}
        </ScrollArea>
      </Box>
    );
  }

  // ── 列表态 ──
  return (
    <Box sx={{ display: "flex", flexDirection: "column", minHeight: 0, height: "100%" }}>
      <Stack direction="row" spacing={0.5} alignItems="center"
        sx={{ px: 1, py: 0.5, flexShrink: 0, borderBottom: "1px solid", borderColor: "divider" }}>
        <Typography variant="subtitle2" sx={{ fontWeight: 600 }}>产物</Typography>
        <Chip size="small" label={arts.length} variant="outlined" sx={{ height: 18, fontSize: 11 }} />
        <Box sx={{ flex: 1 }} />
        {loading && <CircularProgress size={14} sx={{ mr: 0.5 }} />}
        <IconButton size="small" title="刷新" onClick={() => { loadArtifacts(); bumpArtifactsVersion(); }}>
          <RefreshRounded fontSize="small" />
        </IconButton>
      </Stack>
      <ScrollArea sx={{ flexGrow: 1, minHeight: 0 }}>
        <Box sx={{ px: 1, py: 0.5 }}>
          {!currentTaskId ? (
            <Typography variant="body2" color="text.secondary" sx={{ p: 1 }}>
              开始对话后，agent 写出的文件（报告 / 代码 / 截图等）会出现在这里。
            </Typography>
          ) : arts.length === 0 ? (
            <Typography variant="body2" color="text.secondary" sx={{ p: 1 }}>
              {running ? "任务执行中——agent 写出文件后会实时出现在这里。" : "本轮暂无产物。agent 通过工具写出的文件（write_file / 截图等）会登记到这里。"}
            </Typography>
          ) : (
            arts.slice().reverse().map((a) => (
              <Box
                key={a.path}
                onClick={() => openDock("files", a.path)}
                sx={{
                  display: "flex", alignItems: "center", gap: 1, px: 1, py: 0.75,
                  borderRadius: 1, cursor: "pointer", "&:hover": { bgcolor: "action.hover" },
                }}
              >
                <ArtifactIcon name={a.name} />
                <Box sx={{ flex: 1, minWidth: 0 }}>
                  <Typography variant="body2" noWrap sx={{ fontSize: 13, fontWeight: 500 }}>
                    {a.name}
                  </Typography>
                  <Tooltip title={a.path}>
                    <Typography variant="caption" color="text.secondary" noWrap sx={{ display: "block", fontSize: 11 }}>
                      {a.path}
                    </Typography>
                  </Tooltip>
                </Box>
                <Chip size="small" label={a.tool || "file"} variant="outlined"
                  sx={{ height: 16, fontSize: 10, flexShrink: 0, "& .MuiChip-label": { px: 0.5 } }} />
                <Typography variant="caption" color="text.secondary" sx={{ flexShrink: 0, fontSize: 11 }}>
                  {fmtBytes(a.bytes)}
                </Typography>
              </Box>
            ))
          )}
        </Box>
      </ScrollArea>
    </Box>
  );
}
