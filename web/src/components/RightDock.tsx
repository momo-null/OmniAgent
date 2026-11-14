// 右侧 dock：宽度可拖（320–720，另受窗口剩余空间约束），记忆在本机 localStorage，
// 收起为 0；与对话区之间只有 1px 的分隔线，那条线本身就是 col-resize 手柄（双击复位 440）。
// 窗口内是浏览器式页签管理：页签平排、选中用底部指示条（不再是圆角药丸+底色块），
// × 只在 hover 出现，最右整窗关闭用与对话顶栏同源的 panel 图标。
// 主页 = 顶部对齐的功能入口 + 常驻底部的产物清单；「新标签页」入口已删（与 + 号同义）。
import { useCallback, useEffect, useRef, useState } from "react";
import type { Theme } from "@mui/material/styles";
import {
  Box,
  IconButton,
  Stack,
  Tooltip,
  Typography,
} from "@mui/material";
import {
  BlocksIcon,
  BrainIcon,
  BugIcon,
  ChevronDownIcon,
  ChevronRightIcon,
  ChevronUpIcon,
  FileIcon,
  FileTextIcon,
  FolderIcon,
  GitCompareArrowsIcon,
  GlobeIcon,
  HouseIcon,
  ImageIcon,
  PanelRightIcon,
  PlusIcon,
  TerminalIcon,
  XIcon,
} from "./icons";
import FilesTab from "./dock/FilesTab.tsx";
import BrowserTab from "./dock/BrowserTab.tsx";
import GitTab from "./dock/GitTab.tsx";
import TerminalTab from "./dock/TerminalTab.tsx";
import PluginFrame from "./dock/PluginFrame.tsx";
import DebugPanel from "./DebugPanel.tsx";
import MemoryPanel from "./debug/MemoryPanel.tsx";
import { useTaskStore, type DockView } from "../store/taskStore.tsx";
import { pluginApi, workspaceApi, type PluginView, type SourceState } from "../api/client.ts";
import { fmtBytes } from "./dock/FilesTab.tsx";
import type { ArtifactInfo } from "../types";

const DOCK_MIN = 320;
const DOCK_MAX = 720;
const DOCK_DEFAULT = 440;
// 拖到最宽也要给对话区留够读得下的宽度（左侧会话栏另占 260）
const DRAWER_W = 260;
const CHAT_MIN = 380;
const WIDTH_KEY = "omniagent.dock.width";

function clampWidth(w: number): number {
  const cap = Math.max(DOCK_MIN, Math.min(DOCK_MAX, window.innerWidth - DRAWER_W - CHAT_MIN));
  return Math.max(DOCK_MIN, Math.min(cap, Math.round(w)));
}

function loadWidth(): number {
  try {
    const raw = localStorage.getItem(WIDTH_KEY);
    const v = raw ? Number(raw) : NaN;
    // 没存过也要夹一次：窄窗口下直接用 440 会把对话区挤成 0 宽
    return clampWidth(Number.isFinite(v) ? v : DOCK_DEFAULT);
  } catch {
    return DOCK_DEFAULT;
  }
}

function saveWidth(w: number): void {
  try {
    localStorage.setItem(WIDTH_KEY, String(w));
  } catch {
    /* 隐私模式等写不进去：本次会话仍然生效，不做提示 */
  }
}

const IMAGE_ART_EXTS = new Set([".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".svg"]);

// 内置视图定义（页签标题 + 主页入口；home 只作页签标题，不再列进主页入口）。
// 插件页面不在这里：它们由 GET /plugins/ui 注入，键 = `plugin:<plugin>:<view>`。
const VIEWS: { key: DockView; label: string; icon: React.ReactNode }[] = [
  { key: "home", label: "主页", icon: <HouseIcon fontSize="small" /> },
  { key: "files", label: "查看产物", icon: <FolderIcon fontSize="small" /> },
  { key: "browser", label: "打开浏览器", icon: <GlobeIcon fontSize="small" /> },
  { key: "git", label: "查看变更", icon: <GitCompareArrowsIcon fontSize="small" /> },
  { key: "memory", label: "记忆", icon: <BrainIcon fontSize="small" /> },
  { key: "terminal", label: "打开终端", icon: <TerminalIcon fontSize="small" /> },
  { key: "debug", label: "Debug", icon: <BugIcon fontSize="small" /> },
];

const PLUGIN_KEY_PREFIX = "plugin:";

// 图标名 → 字形的小词表；宿主不认识具体插件，缺省一律方块图标。
const PLUGIN_ICONS: Record<string, React.ReactNode> = {
  blocks: <BlocksIcon fontSize="small" />,
  image: <ImageIcon fontSize="small" />,
  file: <FileIcon fontSize="small" />,
  terminal: <TerminalIcon fontSize="small" />,
  brain: <BrainIcon fontSize="small" />,
  globe: <GlobeIcon fontSize="small" />,
};

function pluginViewKey(v: PluginView): DockView {
  return `${PLUGIN_KEY_PREFIX}${v.plugin}:${v.id}` as DockView;
}

function parsePluginView(view: DockView): { plugin: string; id: string } | null {
  if (!view.startsWith(PLUGIN_KEY_PREFIX)) return null;
  const rest = view.slice(PLUGIN_KEY_PREFIX.length);
  const i = rest.indexOf(":");
  if (i <= 0) return null;
  return { plugin: rest.slice(0, i), id: rest.slice(i + 1) };
}

function viewMeta(view: DockView, plugins: PluginView[] = []) {
  const builtin = VIEWS.find((v) => v.key === view);
  if (builtin) return builtin;
  const pv = plugins.find((v) => pluginViewKey(v) === view);
  if (pv) {
    return { key: view, label: pv.label, icon: PLUGIN_ICONS[pv.icon] ?? <BlocksIcon fontSize="small" /> };
  }
  const parsed = parsePluginView(view);
  return { key: view, label: parsed ? parsed.plugin : String(view), icon: <BlocksIcon fontSize="small" /> };
}

// 注入的插件视图 + 事件源状态（30s 轮询 + 窗口聚焦时刷新；读不到就空表，不猜）
function usePluginViews() {
  const [views, setViews] = useState<PluginView[]>([]);
  const [sources, setSources] = useState<Record<string, SourceState>>({});
  const [note, setNote] = useState("");
  const load = useCallback(() => {
    pluginApi.ui()
      .then((r) => setViews((r.data?.views || []).filter((v) => v.slot === "dock" && v.enabled)))
      .catch(() => setViews([]));
    pluginApi.sources()
      .then((r) => {
        const map: Record<string, SourceState> = {};
        (r.data?.sources || []).forEach((s) => { map[s.name] = s; });
        setSources(map);
        setNote(r.data?.note || "");
      })
      .catch(() => {});
  }, []);
  useEffect(() => {
    load();
    const t = setInterval(load, 30000);
    window.addEventListener("focus", load);
    return () => { clearInterval(t); window.removeEventListener("focus", load); };
  }, [load]);
  return { views, sources, note };
}

function artifactIcon(name: string) {
  const i = name.lastIndexOf(".");
  const ext = i >= 0 ? name.slice(i).toLowerCase() : "";
  if (IMAGE_ART_EXTS.has(ext)) return <ImageIcon fontSize="small" sx={{ color: "fileImage.main" }} />;
  if (ext === ".md" || ext === ".markdown") return <FileTextIcon fontSize="small" sx={{ color: "fileDoc.main" }} />;
  return <FileIcon fontSize="small" sx={{ color: "text.disabled" }} />;
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

// ── 主页：入口顶部对齐，产物清单常驻底部 ────────────────────────────────
function HomeView({ onNavigate, pluginViews, sources, note }:
  { onNavigate: (view: Exclude<DockView, "home">) => void;
    pluginViews: PluginView[]; sources: Record<string, SourceState>; note: string }) {
  const { arts, running, hasTask } = useArtifacts();
  const { openDock } = useTaskStore();
  const [listOpen, setListOpen] = useState(true);
  const entrySx = {
    display: "flex", alignItems: "center", gap: 1.25, px: 1.5, minHeight: 34,
    cursor: "pointer", mx: 0.5, borderRadius: 1, color: "text.primary",
    "&:hover": { bgcolor: "action.hover", "& .dock-go": { opacity: 1 } },
  } as const;
  const entries = [
    ...VIEWS.filter((v) => v.key !== "home"),
    ...pluginViews.map((v) => ({
      key: pluginViewKey(v), label: v.label, icon: PLUGIN_ICONS[v.icon] ?? <BlocksIcon fontSize="small" />,
      plugin: v.plugin,
    })),
  ];
  return (
    <Box sx={{ height: "100%", display: "flex", flexDirection: "column", py: 1 }}>
      <Typography variant="caption" sx={{ px: 2, pt: 0.5, pb: 0.5, color: "text.secondary", fontWeight: 500 }}>
        打开
      </Typography>
      {entries.map((t) => {
        const src = "plugin" in t ? sources[t.plugin] : undefined;
        return (
          <Box key={t.key} role="button" tabIndex={0} onClick={() => onNavigate(t.key as Exclude<DockView, "home">)}
               onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onNavigate(t.key as Exclude<DockView, "home">); } }}
               sx={entrySx} title={t.label}>
            {t.icon}
            <Typography variant="body2" sx={{ fontSize: 13.5, flex: 1, minWidth: 0 }}>{t.label}</Typography>
            {"plugin" in t && (
              <Tooltip title={src ? `${src.state}${src.error ? `：${src.error}` : ""}` : (note || "事件源状态未报告")} placement="left">
                <Box sx={{
                  width: 6, height: 6, borderRadius: "50%", flexShrink: 0,
                  bgcolor: !src ? "text.disabled" : src.state === "connected"
                    ? (src.probe === "ready" || src.probe === "unknown" ? "success.main" : "warning.main")
                    : src.state === "starting" ? "warning.main" : "error.main",
                }} />
              </Tooltip>
            )}
            <ChevronRightIcon className="dock-go" sx={{ fontSize: 16, color: "text.disabled", opacity: 0 }} />
          </Box>
        );
      })}
      {/* 产物清单：贴住底部，面板拉高时不再把入口顶到中间 */}
      <Box
        role="button" tabIndex={0}
        onClick={() => setListOpen((o) => !o)}
        onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setListOpen((o) => !o); } }}
        sx={{ ...entrySx, mt: "auto", pt: 1, pb: 0.5, minHeight: 30, userSelect: "none", borderTop: "1px solid", borderColor: "divider", borderRadius: 0, mx: 0, px: 1.5 }}
        title="产物"
      >
        <Typography variant="caption" sx={{ color: "text.secondary", fontWeight: 500 }}>
          产物（{arts.length}）
        </Typography>
        <Box sx={{ flex: 1 }} />
        {listOpen
          ? <ChevronUpIcon sx={{ fontSize: 16, color: "text.disabled" }} />
          : <ChevronDownIcon sx={{ fontSize: 16, color: "text.disabled" }} />}
      </Box>
      {listOpen && (
        <Box sx={{ flex: "0 1 auto", minHeight: 0, overflowY: "auto", px: 0.5, pb: 1 }}>
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
                    display: "flex", alignItems: "center", gap: 0.75, px: 1.5, minHeight: 28,
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

function DockBody({ view, pluginViews, sources, note }:
  { view: DockView; pluginViews: PluginView[]; sources: Record<string, SourceState>; note: string }) {
  const { debugLogs, clearDebugLogs } = useTaskStore();
  const parsed = parsePluginView(view);
  if (parsed) {
    const meta = viewMeta(view, pluginViews);
    const pv = pluginViews.find((v) => pluginViewKey(v) === view);
    return (
      <PluginFrame label={meta.label} url={pv?.url || ""} source={sources[parsed.plugin]} note={note} />
    );
  }
  switch (view) {
    case "files": return <FilesTab />;
    case "browser": return <BrowserTab />;
    case "git": return <GitTab />;
    case "memory": return <MemoryPanel />;
    case "terminal": return <TerminalTab />;
    case "debug":
    default: return <DebugPanel logs={debugLogs} onClear={clearDebugLogs} />;
  }
}

export default function RightDock() {
  const plugins = usePluginViews();
  const {
    dockOpen, dockTabs, dockActiveId,
    closeDock, newDockTab, closeDockTab, activateDockTab, navigateDockTab,
  } = useTaskStore();
  const [width, setWidthState] = useState<number>(loadWidth);
  const [dragging, setDragging] = useState(false);
  const dragRef = useRef<{ startX: number; startW: number } | null>(null);
  // 松手时要落盘的是「拖到哪」，不能读渲染闭包里的 width：一次拖动内 React 可能还没重渲染
  const widthRef = useRef(width);
  const setWidth = useCallback((w: number) => {
    const v = clampWidth(w);
    widthRef.current = v;
    setWidthState(v);
    return v;
  }, []);

  const active = dockTabs.find((t) => t.id === dockActiveId);

  // 窗口变窄时把已记忆的宽度重新夹回可用范围（否则对话区会被压到读不下）
  useEffect(() => {
    const onResize = () => setWidth(widthRef.current);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [setWidth]);

  // 宽度必须写成字符串：MUI 的 sx 会把 ≤1 的数字 width 换算成百分比（1 → 100%），
  // 1px 的分隔线要是写成数字就会被撑成整条热区，正好是之前「界线太宽」的根因。
  const widthSx = (w: number) => ({
    width: dockOpen ? `${w}px` : 0,
    flexShrink: 0, minHeight: 0, overflow: "hidden",
    // 拖动时必须关掉宽度过渡，否则面板跟不上手
    transition: dragging ? "none" : (t: Theme) => t.transitions.create("width", {
      easing: t.transitions.easing.sharp,
      duration: dockOpen ? t.transitions.duration.enteringScreen : t.transitions.duration.leavingScreen,
    }),
  });

  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!dockOpen) return;
    dragRef.current = { startX: e.clientX, startW: widthRef.current };
    setDragging(true);
    e.currentTarget.setPointerCapture?.(e.pointerId);
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
  };
  const onPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const d = dragRef.current;
    if (!d) return;
    // 手柄在左、面板在右：往左拖加宽
    setWidth(d.startW - (e.clientX - d.startX));
  };
  const endDrag = () => {
    if (!dragRef.current) return;
    dragRef.current = null;
    setDragging(false);
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
    saveWidth(widthRef.current);
  };

  return (
    <>
      {/* 边界：一条 1px 的线，本身就是可拖动手柄（命中区靠 ::before 向两侧各扩 3px，
          所以鼠标提前碰到也认）；hover/拖动染成主色，松手回分隔线色。dock 面板不再画
          自己的左边线，否则松手后会看到紧贴的两条线。 */}
      <Box
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
        onDoubleClick={() => saveWidth(setWidth(DOCK_DEFAULT))}
        role="separator"
        aria-orientation="vertical"
        aria-label="拖动调整右侧面板宽度，双击恢复默认"
        aria-valuemin={DOCK_MIN}
        aria-valuemax={DOCK_MAX}
        aria-valuenow={dockOpen ? width : undefined}
        sx={{
          ...widthSx(1), overflow: "visible", cursor: "col-resize", position: "relative",
          touchAction: "none",
          bgcolor: dragging ? "primary.main" : "divider",
          "&:hover": { bgcolor: "primary.main" },
          "&::before": { content: '""', position: "absolute", top: 0, bottom: 0, left: -3, right: -3 },
        }}
      />

      <Box sx={{ ...widthSx(width), bgcolor: "background.default" }}>
        <Box sx={{ width: "100%", height: "100%", minHeight: 0, display: "flex", flexDirection: "column" }}>
          {/* 页签条：平排，选中提亮文字 + 底部 2px 指示条；× 仅 hover 现，最右整窗关闭 */}
          <Stack
            direction="row"
            alignItems="flex-end"
            spacing={0}
            sx={{ px: 0.5, flexShrink: 0, minHeight: 36, borderBottom: "1px solid", borderColor: "divider" }}
          >
            {dockTabs.map((t) => {
              const meta = viewMeta(t.view, plugins.views);
              const isActive = t.id === dockActiveId;
              return (
                <Stack
                  key={t.id}
                  direction="row"
                  spacing={0.5}
                  alignItems="center"
                  onClick={() => activateDockTab(t.id)}
                  sx={{
                    position: "relative", maxWidth: 170, minWidth: 0, pl: 1.25, pr: 0.75, minHeight: 34,
                    cursor: "pointer", color: isActive ? "text.primary" : "text.secondary",
                    "&:hover": { color: "text.primary", bgcolor: "action.hover" },
                    "&:hover .dock-tab-x": { opacity: 1 },
                    ...(isActive && {
                      "&::after": {
                        content: '""', position: "absolute", left: 10, right: 10, bottom: 0,
                        height: 2, bgcolor: "primary.main",
                      },
                    }),
                  }}
                  title={meta.label}
                >
                  {meta.icon}
                  <Typography
                    variant="caption"
                    noWrap
                    sx={{ fontSize: 12, flexShrink: 1, fontWeight: isActive ? 500 : 400 }}
                  >
                    {meta.label}
                  </Typography>
                  <IconButton
                    size="small"
                    className="dock-tab-x"
                    onClick={(e) => { e.stopPropagation(); closeDockTab(t.id); }}
                    aria-label={`关闭 ${meta.label}`}
                    sx={{
                      width: 18, height: 18, p: 0, opacity: 0, color: "text.disabled",
                      transition: "opacity .12s", "&:hover": { color: "text.primary", bgcolor: "transparent" },
                    }}
                  >
                    <XIcon sx={{ fontSize: 13 }} />
                  </IconButton>
                </Stack>
              );
            })}
            <Tooltip title="新建页签">
              <IconButton
                onClick={newDockTab}
                aria-label="新建页签"
                size="small"
                sx={{ width: 28, height: 26, borderRadius: 1, color: "text.secondary", "&:hover": { bgcolor: "action.hover" } }}
              >
                <PlusIcon sx={{ fontSize: 16 }} />
              </IconButton>
            </Tooltip>
            <Box sx={{ flex: 1 }} />
            <IconButton
              onClick={closeDock}
              size="small"
              title="关闭侧栏"
              aria-label="关闭侧栏"
              sx={{
                width: 30, height: 22, borderRadius: "4px", p: 0, mr: 0.25, color: "text.secondary",
                "&:hover": { bgcolor: "action.hover", color: "text.primary" },
              }}
            >
              <PanelRightIcon sx={{ fontSize: 16 }} />
            </IconButton>
          </Stack>
          {/* 内容区：主页 = 入口 list；其余视图渲染对应功能面板 */}
          <Box sx={{ flex: 1, minHeight: 0, overflow: "hidden" }}>
            {!active || active.view === "home" ? (
              <HomeView onNavigate={navigateDockTab} pluginViews={plugins.views}
                        sources={plugins.sources} note={plugins.note} />
            ) : (
              <DockBody view={active.view} pluginViews={plugins.views}
                        sources={plugins.sources} note={plugins.note} />
            )}
          </Box>
        </Box>
      </Box>
    </>
  );
}
