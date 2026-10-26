// 右侧 dock 窗口：由 chat 顶栏开关整体收起/展开（固定 440px，不做多级窗口）。
// 窗口内是浏览器式页签管理：页签条（图标+标题+×，+ 新建页签默认主页），
// 主页 = 功能入口在窗口中部 list 排列 + 产物清单；点入口当前页签跳转到功能视图。
import { useCallback, useEffect, useState } from "react";
import {
  Box,
  IconButton,
  Stack,
  Tooltip,
  Typography,
} from "@mui/material";
import AddRounded from "@mui/icons-material/AddRounded";
import BugReportRounded from "@mui/icons-material/BugReportRounded";
import CloseRounded from "@mui/icons-material/CloseRounded";
import DescriptionRounded from "@mui/icons-material/DescriptionRounded";
import DifferenceRounded from "@mui/icons-material/DifferenceRounded";
import ExpandLessRounded from "@mui/icons-material/ExpandLessRounded";
import ExpandMoreRounded from "@mui/icons-material/ExpandMoreRounded";
import FolderRounded from "@mui/icons-material/FolderRounded";
import HomeRounded from "@mui/icons-material/HomeRounded";
import ImageRounded from "@mui/icons-material/ImageRounded";
import InsertDriveFileRounded from "@mui/icons-material/InsertDriveFileRounded";
import PublicRounded from "@mui/icons-material/PublicRounded";
import TerminalRounded from "@mui/icons-material/TerminalRounded";
import FilesTab from "./dock/FilesTab.tsx";
import BrowserTab from "./dock/BrowserTab.tsx";
import GitTab from "./dock/GitTab.tsx";
import TerminalTab from "./dock/TerminalTab.tsx";
import DebugPanel from "./DebugPanel.tsx";
import { useTaskStore, type DockTab, type DockView } from "../store/taskStore.tsx";
import { workspaceApi } from "../api/client.ts";
import { fmtBytes } from "./dock/FilesTab.tsx";
import type { ArtifactInfo } from "../types";

const WIN_W = 440;

const IMAGE_ART_EXTS = new Set([".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".svg"]);

// 各视图定义（主页 + 功能入口；顺序即主页展示顺序）
const VIEWS: { key: DockView; label: string; icon: React.ReactNode }[] = [
  { key: "home", label: "新标签页", icon: <HomeRounded fontSize="small" /> },
  { key: "files", label: "查看产物", icon: <FolderRounded fontSize="small" /> },
  { key: "browser", label: "打开浏览器", icon: <PublicRounded fontSize="small" /> },
  { key: "git", label: "查看变更", icon: <DifferenceRounded fontSize="small" /> },
  { key: "terminal", label: "打开终端", icon: <TerminalRounded fontSize="small" /> },
  { key: "debug", label: "Debug", icon: <BugReportRounded fontSize="small" /> },
];

function viewMeta(view: DockView) {
  return VIEWS.find((v) => v.key === view) ?? VIEWS[0];
}

function artifactIcon(name: string) {
  const i = name.lastIndexOf(".");
  const ext = i >= 0 ? name.slice(i).toLowerCase() : "";
  if (IMAGE_ART_EXTS.has(ext)) return <ImageRounded fontSize="small" sx={{ color: "#42a5f5" }} />;
  if (ext === ".md" || ext === ".markdown") return <DescriptionRounded fontSize="small" sx={{ color: "#26c6da" }} />;
  return <InsertDriveFileRounded fontSize="small" sx={{ color: "text.disabled" }} />;
}

// 产物登记表轮询（主页清单与文件页同源；运行中 3s 轮询，新产物实时出现）
function useArtifacts() {
  const { currentTaskId, running } = useTaskStore();
  const [arts, setArts] = useState<ArtifactInfo[]>([]);
  const load = useCallback(() => {
    if (!currentTaskId) { setArts([]); return; }
    workspaceApi.artifacts(currentTaskId)
      .then((r) => setArts((r.data as { artifacts?: ArtifactInfo[] })?.artifacts || []))
      .catch(() => {});
  }, [currentTaskId]);
  useEffect(() => {
    load();
    if (!running || !currentTaskId) return;
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, [load, running, currentTaskId]);
  return { arts, running, hasTask: !!currentTaskId };
}

// ── 主页：功能入口在窗口中部 list 排列 + 产物 (N) 可折叠清单 ──────────────
function HomeView({ onNavigate }: { onNavigate: (view: DockTab) => void }) {
  const { arts, running, hasTask } = useArtifacts();
  const { openDock } = useTaskStore();
  const [listOpen, setListOpen] = useState(true);
  const rowSx = {
    display: "flex", alignItems: "center", gap: 1.25, px: 1.5, py: 1.25,
    cursor: "pointer", mx: 0.5, borderBottom: "1px solid", borderColor: "divider",
    "&:hover": { bgcolor: "action.hover" },
  } as const;
  return (
    <Box sx={{ height: "100%", display: "flex", flexDirection: "column", justifyContent: "center", py: 2 }}>
      {VIEWS.filter((v) => v.key !== "home").map((t) => (
        <Box key={t.key} onClick={() => onNavigate(t.key as DockTab)} sx={rowSx} title={t.label}>
          {t.icon}
          <Typography variant="body2" sx={{ fontSize: 13.5, color: "text.primary" }}>{t.label}</Typography>
        </Box>
      ))}
      {/* 产物清单（点击产物行 → 跳到文件页并预览该文件） */}
      <Box
        onClick={() => setListOpen((o) => !o)}
        sx={{ ...rowSx, mt: 0.5, cursor: "pointer", userSelect: "none" }}
        title="产物"
      >
        <Typography variant="caption" color="text.secondary" sx={{ fontWeight: 600 }}>
          产物（{arts.length}）
        </Typography>
        <Box sx={{ flex: 1 }} />
        {listOpen
          ? <ExpandLessRounded fontSize="small" sx={{ color: "text.disabled" }} />
          : <ExpandMoreRounded fontSize="small" sx={{ color: "text.disabled" }} />}
      </Box>
      {listOpen && (
        <Box sx={{ flex: 1, minHeight: 0, overflowY: "auto", px: 0.5 }}>
          {arts.length === 0 ? (
            <Typography variant="caption" color="text.secondary" sx={{ px: 1.5, display: "block" }}>
              {!hasTask
                ? "开始对话后，agent 写出的文件会出现在这里"
                : running
                  ? "任务执行中，产物实时刷新…"
                  : "本轮暂无产物"}
            </Typography>
          ) : (
            arts.slice().reverse().map((a) => (
              <Tooltip key={a.path} title={a.path} placement="left">
                <Box
                  onClick={() => openDock("files", a.path)}
                  sx={{
                    display: "flex", alignItems: "center", gap: 0.75, px: 1.5, py: 0.5,
                    cursor: "pointer", borderRadius: 1, "&:hover": { bgcolor: "action.hover" },
                  }}
                >
                  {artifactIcon(a.name)}
                  <Typography variant="body2" noWrap sx={{ flex: 1, minWidth: 0, fontSize: 12.5 }}>
                    {a.name}
                  </Typography>
                  <Typography variant="caption" color="text.secondary" sx={{ flexShrink: 0, fontSize: 10.5 }}>
                    {fmtBytes(a.bytes)}
                  </Typography>
                </Box>
              </Tooltip>
            ))
          )}
        </Box>
      )}
    </Box>
  );
}

function DockBody({ view }: { view: DockTab }) {
  const { debugLogs, clearDebugLogs, currentTaskId } = useTaskStore();
  switch (view) {
    case "files": return <FilesTab />;
    case "browser": return <BrowserTab />;
    case "git": return <GitTab />;
    case "terminal": return <TerminalTab />;
    case "debug":
    default: return <DebugPanel logs={debugLogs} onClear={clearDebugLogs} taskId={currentTaskId} />;
  }
}

export default function RightDock() {
  const {
    dockOpen, dockTabs, dockActiveId, openDockMenu,
    closeDock, newDockTab, closeDockTab, activateDockTab, navigateDockTab,
  } = useTaskStore();
  const active = dockTabs.find((t) => t.id === dockActiveId);

  return (
    // 固定宽度窗口（收起 0 / 展开 440）：width 过渡 + 内层定宽裁切，
    // 聊天区（flexGrow 兄弟）同步回流。
    <Box
      sx={{
        width: dockOpen ? WIN_W : 0,
        flexShrink: 0, minHeight: 0, overflow: "hidden",
        borderLeft: dockOpen ? "1px solid" : "none", borderColor: "divider",
        bgcolor: "background.default",
        transition: (t) => t.transitions.create("width", {
          easing: t.transitions.easing.sharp,
          duration: dockOpen ? t.transitions.duration.enteringScreen : t.transitions.duration.leavingScreen,
        }),
      }}
    >
      {dockOpen && (
        <Box sx={{ width: WIN_W, height: "100%", minHeight: 0, display: "flex", flexDirection: "column" }}>
          {/* 页签条：浏览器式（图标+标题+×），+ 新建页签（默认主页），最右整窗关闭 */}
          <Stack
            direction="row"
            alignItems="flex-end"
            spacing={0.25}
            sx={{ px: 0.5, pt: 0.5, flexShrink: 0, borderBottom: "1px solid", borderColor: "divider" }}
          >
            {dockTabs.map((t) => {
              const meta = viewMeta(t.view);
              const isActive = t.id === dockActiveId;
              return (
                <Stack
                  key={t.id}
                  direction="row"
                  spacing={0.25}
                  alignItems="center"
                  onClick={() => activateDockTab(t.id)}
                  sx={{
                    maxWidth: 150, minWidth: 0, px: 1, py: 0.5,
                    cursor: "pointer", borderRadius: "6px 6px 0 0",
                    bgcolor: isActive ? "action.selected" : "transparent",
                    "&:hover": { bgcolor: isActive ? "action.selected" : "action.hover" },
                  }}
                  title={meta.label}
                >
                  {meta.icon}
                  <Typography
                    variant="caption"
                    noWrap
                    sx={{ fontSize: 12, color: isActive ? "text.primary" : "text.secondary", flexShrink: 1 }}
                  >
                    {meta.label}
                  </Typography>
                  <IconButton
                    size="small"
                    onClick={(e) => { e.stopPropagation(); closeDockTab(t.id); }}
                    aria-label={`关闭 ${meta.label}`}
                    sx={{ width: 16, height: 16, p: 0, ml: 0.25, color: "text.disabled", "&:hover": { color: "text.primary" } }}
                  >
                    <CloseRounded sx={{ fontSize: 13 }} />
                  </IconButton>
                </Stack>
              );
            })}
            <Tooltip title="新建页签">
              <IconButton
                onClick={newDockTab}
                aria-label="新建页签"
                size="small"
                sx={{ width: 26, height: 22, borderRadius: 1, color: "text.secondary", "&:hover": { bgcolor: "action.hover" } }}
              >
                <AddRounded sx={{ fontSize: 16 }} />
              </IconButton>
            </Tooltip>
            <Box sx={{ flex: 1 }} />
            <IconButton
              onClick={closeDock}
              title="关闭侧栏"
              aria-label="关闭侧栏"
              size="small"
              sx={{ width: 26, height: 22, borderRadius: 1, color: "text.secondary", "&:hover": { bgcolor: "action.hover" } }}
            >
              <CloseRounded sx={{ fontSize: 16 }} />
            </IconButton>
          </Stack>
          {/* 内容区：主页 = 入口 list；其余视图渲染对应功能面板 */}
          <Box sx={{ flex: 1, minHeight: 0, overflow: "hidden" }}>
            {!active || active.view === "home" ? (
              <HomeView onNavigate={navigateDockTab} />
            ) : (
              <DockBody view={active.view} />
            )}
          </Box>
        </Box>
      )}
    </Box>
  );
}
