import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Box,
  Drawer,
  List,
  ListItemButton,
  ListItemIcon,
  ListItemText,
  Toolbar,
  Typography,
  Divider,
  Collapse,
  IconButton,
  Menu,
  MenuItem,
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  Button,
  TextField,
} from "@mui/material";
import {
  ChevronDownIcon,
  ChevronUpIcon,
  ClockIcon,
  FolderIcon,
  FolderOpenIcon,
  PencilIcon,
  PlusIcon,
  SaveIcon,
  SettingsIcon,
  TrashIcon,
  WrenchIcon,
} from "./icons";
import CronJobs from "../pages/CronJobs";
import { taskApi, projectApi } from "../api/client";
import { notifyModelsChanged } from "../api/modelEvents";
import { useTaskStore, type MainView } from "../store/taskStore.tsx";
import Chat from "../pages/Chat";
import SkillsAndTools from "../pages/SkillsAndTools";
import Settings from "../pages/Settings";

const DRAWER_WIDTH = 260;

// 侧栏与设置/技能工具遮罩的左栏同规格：圆角 2、图标位 28、未选中图标降一档亮度，选中回到 text.primary。
// 图标整体柔和度仍由全站唯一开关 --omni-icon-opacity（外观 · 图标浓度）负责，这里不再各自加透明度。
const ROW_SX = {
  mb: 0.25, py: 0.7, borderRadius: 2,
  "& .MuiListItemIcon-root": { minWidth: 28, color: "text.secondary" },
  "&.Mui-selected .MuiListItemIcon-root": { color: "text.primary" },
};
// 项目/会话行更密，只收纵向留白，其余规格与入口行一致
const ROW_DENSE_SX = { ...ROW_SX, py: 0.25, mb: 0 };
// 文字一律用 rem：外观 · 字号 缩放时侧栏与设置页一起走（标准档 = 今天的像素值）
const LABEL_NAV = { fontSize: "1rem" };        // 14px
const LABEL_ROW = { fontSize: "0.9286rem" };   // 13px
const LABEL_CHILD = { fontSize: "0.8571rem" }; // 12px：项目下的会话，比父行矮一档
const LABEL_META = { fontSize: "0.7857rem" };  // 11px：副标题 / N 任务 / 行尾相对时间

// git 式相对时间（任务行行尾）：刚刚 / N分钟 / N小时 / N天，超过 30 天回退日期。
// 入参是后端 last_active_at（ISO，UTC 带时区），解析失败/缺了返回空（不显示）。
function relTime(iso?: string): string {
  if (!iso) return "";
  const t = new Date(iso).getTime();
  if (!Number.isFinite(t)) return "";
  const min = Math.floor(Math.max(0, Date.now() - t) / 60000);
  if (min < 1) return "刚刚";
  if (min < 60) return `${min}分钟`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}小时`;
  const day = Math.floor(hr / 24);
  if (day <= 30) return `${day}天`;
  const d = new Date(t);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

export default function Layout() {
  const {
    view, setView, tasks, currentTaskId, selectTask,
    refreshTasks, connectStream, disconnectStream, taskTitles, taskObjectives,
    taskProjects, taskTimes, deleteTask, renameTask, runningByTask,
  } = useTaskStore();

  const [tasksOpen, setTasksOpen] = useState(true);
  // 设置与技能工具都是盖住整窗的遮罩，不是一种主视图：底稿停在用户原来那一页，关掉即回到原处
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [skillsOpen, setSkillsOpen] = useState(false);

  // 知识分层 C4/C5：项目侧栏 + 保存到项目
  const [projects, setProjects] = useState<{ id: string; display_name: string; session_count: number }[]>([]);
  const [projectsOpen, setProjectsOpen] = useState(true);
  // 项目行的展开态（缺省展开）：保存到项目的任务嵌套挂在项目下
  const [openProjects, setOpenProjects] = useState<Record<string, boolean>>({});
  const [saveOpen, setSaveOpen] = useState(false);
  const [saveNewProject, setSaveNewProject] = useState("");
  // 右键菜单定位（项目行）：记鼠标坐标，Menu 用 anchorPosition 在点击处弹出
  const [projMenuPos, setProjMenuPos] = useState<{ mouseX: number; mouseY: number } | null>(null);
  const [projMenuId, setProjMenuId] = useState("");

  const refreshProjects = () =>
    projectApi.meta()
      .then((r) => setProjects(r.data?.projects || []))
      .catch(() => { /* 忽略瞬时错误 */ });

  useEffect(() => { refreshProjects(); }, []);

  // 任务右键菜单状态：右键行任意位置弹出（坐标即弹出点），不再有 3 点按钮
  const [menuPos, setMenuPos] = useState<{ mouseX: number; mouseY: number } | null>(null);
  const [menuTaskId, setMenuTaskId] = useState("");
  // 重命名对话框状态（任务）
  const [renameOpen, setRenameOpen] = useState(false);
  const [renameValue, setRenameValue] = useState("");
  // 重命名对话框状态（项目）：只改 display_name（C3），目录 slug 不动
  const [projRenameOpen, setProjRenameOpen] = useState(false);
  const [projRenameId, setProjRenameId] = useState("");
  const [projRenameValue, setProjRenameValue] = useState("");

  // 初始化任务列表（仅一次）
  useEffect(() => {
    refreshTasks();
  }, [refreshTasks]);

  // SSE 跟随当前 task 订阅：currentTaskId 变化即重连到对应 task 的事件流，
  // 解决「发消息拿到新 tid / 新建对话切到 "" 后前端收不到消息」的问题。
  useEffect(() => {
    connectStream();
    return () => disconnectStream();
  }, [currentTaskId, connectStream, disconnectStream]);

  const displayNameOf = (pid: string) =>
    projects.find((p) => p.id === pid)?.display_name || pid;

  // 任务显示名：空 objective（「新建会话」实体，首条消息后自动命名）回退固定标题
  const taskLabel = useCallback((t: string) =>
    taskTitles[t] || taskObjectives[t] || "新会话", [taskTitles, taskObjectives]);

  // default 是内核的缺省归属（不建项目的 task 都归它），不是用户创建的实体——
  // UI 不展示（「用户不创建项目就不该存在」）；任务仍可归属它，只是不标名。
  const userProjects = projects.filter((p) => p.id !== "default");

  // 保存到用户项目的任务嵌套挂在项目下；扁平「任务」区只留未归属（default/无/悬空）任务
  const userProjectIds = useMemo(() => new Set(userProjects.map((p) => p.id)), [userProjects]);
  const tasksOfProject = useCallback(
    (pid: string) => tasks.filter((t) => taskProjects[t] === pid),
    [tasks, taskProjects],
  );
  const flatTasks = useMemo(
    () => tasks.filter((t) => {
      const pid = taskProjects[t];
      return !pid || !userProjectIds.has(pid);
    }),
    [tasks, taskProjects, userProjectIds],
  );

  const saveToProject = async (pid: string, display?: string) => {
    const id = menuTaskId;
    if (!id || !pid) return;
    try {
      if (!projects.some((p) => p.id === pid)) {
        await projectApi.create({ project_id: pid, display_name: display || pid });
      }
      await taskApi.updateState(id, { project_id: pid });
      setSaveOpen(false);
      setSaveNewProject("");
      refreshProjects();
      refreshTasks();
    } catch (e: unknown) {
      const err = e as { response?: { data?: { error?: string } }; message?: string };
      window.alert(`保存到项目失败：${err?.response?.data?.error || err?.message || e}`);
    }
  };

  const createAndSave = () => {
    const raw = saveNewProject.trim();
    if (!raw) return;
    // 目录 id 只允许 [A-Za-z0-9_-]（validate_identifier）；中文/空格走显示名
    const legal = raw.replace(/[^A-Za-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "");
    const pid = legal || `p-${Date.now().toString(36)}`;
    void saveToProject(pid, raw);
  };

  const deleteProject = async (pid: string) => {
    const n = Object.values(taskProjects).filter((v) => v === pid).length;
    const assets = "该项目目录下的全部会话与项目级知识（技能/记忆）将一并删除。";
    if (!window.confirm(`确定删除项目「${displayNameOf(pid)}」？
将级联删除其中 ${n} 个任务。
${assets}`)) {
      return;
    }
    try {
      const r = await projectApi.remove(pid);
      if (!r.data?.ok) { window.alert(`删除失败：${r.data?.error || "未知错误"}`); return; }
      if ((r.data?.deleted_tasks ?? 0) !== n) {
        window.alert(`项目已删除（级联删除 ${r.data?.deleted_tasks ?? 0} 个任务）`);
      }
      refreshProjects();
      refreshTasks();
    } catch (e: unknown) {
      const err = e as { response?: { data?: { error?: string } }; message?: string };
      window.alert(`删除失败：${err?.response?.data?.error || err?.message || e}`);
    }
  };

  // 重命名项目：只改展示名（display_name 存 project.json），目录 slug 不变，
  // 因此任务归属 / chat_routes 全都不受影响
  const renameProject = async () => {
    const pid = projRenameId;
    const name = projRenameValue.trim();
    if (!pid || !name) return;
    try {
      const r = await projectApi.setAlias(pid, name);
      if (!r.data?.ok) { window.alert(`重命名失败：${r.data?.error || "未知错误"}`); return; }
      setProjRenameOpen(false);
      refreshProjects();
    } catch (e: unknown) {
      const err = e as { response?: { data?: { error?: string } }; message?: string };
      window.alert(`重命名失败：${err?.response?.data?.error || err?.message || e}`);
    }
  };

  const renderMain = () => {
    switch (view) {
      case "cron": return <CronJobs />;
      case "chat":
      default: return <Chat />;
    }
  };

  const sidebarEntry = (v: MainView, label: string, icon: React.ReactNode) => (
    <ListItemButton selected={view === v} onClick={() => setView(v)} sx={ROW_SX}>
      <ListItemIcon>{icon}</ListItemIcon>
      <ListItemText primary={label} primaryTypographyProps={LABEL_NAV} />
    </ListItemButton>
  );

  // 遮罩类入口（设置 / 技能与工具）：选中态跟着开合走，关掉即回落到原来那页
  const overlayEntry = (open: boolean, label: string, icon: React.ReactNode, onClick: () => void) => (
    <ListItemButton selected={open} onClick={onClick} sx={ROW_SX}>
      <ListItemIcon>{icon}</ListItemIcon>
      <ListItemText primary={label} primaryTypographyProps={LABEL_NAV} />
    </ListItemButton>
  );

  return (
    <Box sx={{ display: "flex", height: "100vh", width: "100%", overflow: "hidden" }}>
      <Drawer
        variant="permanent"
        sx={{
          width: DRAWER_WIDTH, flexShrink: 0,
          "& .MuiDrawer-paper": {
            width: DRAWER_WIDTH, boxSizing: "border-box",
            display: "flex", flexDirection: "column", overflow: "hidden",
            // 底色保留默认暗色档（比内容区亮一档，层级靠亮度差），值收进 theme.background.sidebar
            borderRight: "none", bgcolor: "background.sidebar",
          },
        }}
      >
        <Toolbar>
          <Typography variant="subtitle1" noWrap sx={{ fontWeight: 600 }}>OmniAgent</Typography>
        </Toolbar>

        {/* 中部滚动区：入口/项目/会话（设置钉在底部，不随内容滚动） */}
        <Box sx={{ flex: 1, minHeight: 0, overflowY: "auto" }}>
        {/* 固定入口 */}
        <List sx={{ py: 0 }}>
          <ListItemButton
            onClick={() => { selectTask(""); setView("chat"); }}
            sx={ROW_SX}
          >
            <ListItemIcon><PlusIcon /></ListItemIcon>
            <ListItemText primary="新建对话" primaryTypographyProps={LABEL_NAV} />
          </ListItemButton>
          {overlayEntry(skillsOpen, "技能与工具", <WrenchIcon />, () => setSkillsOpen(true))}
          {sidebarEntry("cron", "定时任务", <ClockIcon />)}
        </List>

        {/* 项目分区（C4）：用户创建的项目（default 为内核缺省归属，不展示）+ 会话数；菜单支持删除（C5） */}
        <Divider sx={{ mt: 2, mb: 0.5, borderColor: "divider" }} />
        <Box
          sx={{
            display: "flex", alignItems: "center", justifyContent: "space-between",
            px: 2, py: 0.5,
          }}
        >
          <Typography variant="subtitle2" color="text.secondary" sx={{ fontWeight: 500 }}>
            项目
          </Typography>
          <IconButton size="small" onClick={() => setProjectsOpen((o) => !o)} sx={{ p: 0.25 }}>
            {projectsOpen ? <ChevronUpIcon fontSize="small" /> : <ChevronDownIcon fontSize="small" />}
          </IconButton>
        </Box>
        <Collapse in={projectsOpen} timeout="auto" unmountOnExit>
          <List dense disablePadding sx={{ pl: 1, pr: 1, maxHeight: 260, overflowY: "auto" }}>
            {userProjects.length === 0 ? (
              <ListItemText
                primary="（暂无项目）"
                primaryTypographyProps={{ variant: "caption", color: "text.secondary", sx: { pl: 2 } }}
              />
            ) : (
              userProjects.map((p) => {
                const projTasks = tasksOfProject(p.id);
                const open = openProjects[p.id] ?? false; // 默认收起：项目下的会话列表展开与否由用户点击控制
                return (
                  <Box key={p.id}>
                    <Box
                      sx={{ display: "flex", alignItems: "center", borderRadius: 2, "&:hover": { bgcolor: "action.hover" } }}
                      onContextMenu={(e) => {
                        e.preventDefault();
                        setProjMenuId(p.id);
                        setProjMenuPos({ mouseX: e.clientX, mouseY: e.clientY });
                      }}
                    >
                      <ListItemButton
                        onClick={() => setOpenProjects((prev) => ({ ...prev, [p.id]: !open }))}
                        sx={{ ...ROW_DENSE_SX, flexGrow: 1, minWidth: 0 }}
                      >
                        <ListItemIcon>
                          {/* 两态跟着下面的 Collapse 走：闭口 → Lucide 的斜口开口。
                              空项目点开也不会有列表，所以恒用闭口 */}
                          {open && projTasks.length > 0
                            ? <FolderOpenIcon fontSize="small" />
                            : <FolderIcon fontSize="small" />}
                        </ListItemIcon>
                        <ListItemText
                          primary={p.display_name || p.id}
                          primaryTypographyProps={{ noWrap: true, sx: LABEL_ROW }}
                          secondary={`${projTasks.length} 任务`}
                          secondaryTypographyProps={{ noWrap: true, variant: "caption", sx: LABEL_META }}
                        />
                      </ListItemButton>
                    </Box>
                    {/* 项目下的任务（保存到项目的任务挂在这里，点击打开会话） */}
                    {projTasks.length > 0 && (
                      <Collapse in={open} timeout="auto" unmountOnExit>
                        <List dense disablePadding sx={{ pl: 3.5, pb: 0.5 }}>
                          {projTasks.map((t) => (
                            <Box
                              key={t}
                              sx={{ display: "flex", alignItems: "center", borderRadius: 2, "&:hover": { bgcolor: "action.hover" } }}
                              onContextMenu={(e) => {
                                e.preventDefault();
                                setMenuTaskId(t);
                                setMenuPos({ mouseX: e.clientX, mouseY: e.clientY });
                              }}
                            >
                              <ListItemButton
                                selected={t === currentTaskId}
                                onClick={() => { selectTask(t); setView("chat"); }}
                                sx={{ ...ROW_DENSE_SX, flexGrow: 1, minWidth: 0 }}
                              >
                                {runningByTask[t] && (
                                  <Box title="运行中" sx={{ width: 6, height: 6, borderRadius: "50%", bgcolor: "success.main", flexShrink: 0, mr: 0.75 }} />
                                )}
                                <ListItemText
                                  primary={taskLabel(t)}
                                  primaryTypographyProps={{ noWrap: true, sx: LABEL_CHILD }}
                                />
                              </ListItemButton>
                              {taskTimes[t] && (
                                <Typography
                                  variant="caption"
                                  title={new Date(taskTimes[t]).toLocaleString()}
                                  sx={{ flexShrink: 0, mr: 1, ...LABEL_META, color: "text.secondary", userSelect: "none" }}
                                >
                                  {relTime(taskTimes[t])}
                                </Typography>
                              )}
                            </Box>
                          ))}
                        </List>
                      </Collapse>
                    )}
                  </Box>
                );
              })
            )}
          </List>
        </Collapse>

        {/* 任务分区：淡分隔线 + 正常大小标题（纯文字，非按钮） */}
        <Divider sx={{ mt: 2, mb: 0.5, borderColor: "divider" }} />
        <Box
          sx={{
            display: "flex", alignItems: "center", justifyContent: "space-between",
            px: 2, py: 0.5,
          }}
        >
          <Typography variant="subtitle2" color="text.secondary" sx={{ fontWeight: 500 }}>
            任务
          </Typography>
          <IconButton size="small" onClick={() => setTasksOpen((o) => !o)} sx={{ p: 0.25 }}>
            {tasksOpen ? <ChevronUpIcon fontSize="small" /> : <ChevronDownIcon fontSize="small" />}
          </IconButton>
        </Box>
        <Collapse in={tasksOpen} timeout="auto" unmountOnExit>
          <List dense disablePadding sx={{ pl: 1, pr: 1, maxHeight: 240, overflowY: "auto" }}>
            {flatTasks.length === 0 ? (
              <ListItemText primary="（暂无任务）" primaryTypographyProps={{ variant: "caption", color: "text.secondary", sx: { pl: 2 } }} />
            ) : (
              flatTasks.map((t: string) => (
                <Box
                  key={t}
                  sx={{ display: "flex", alignItems: "center", borderRadius: 2, "&:hover": { bgcolor: "action.hover" } }}
                  onContextMenu={(e) => {
                    e.preventDefault();
                    setMenuTaskId(t);
                    setMenuPos({ mouseX: e.clientX, mouseY: e.clientY });
                  }}
                >
                  <ListItemButton
                    selected={t === currentTaskId}
                    onClick={() => { selectTask(t); setView("chat"); }}
                    sx={{ ...ROW_DENSE_SX, flexGrow: 1, minWidth: 0 }}
                  >
                    {runningByTask[t] && (
                      <Box title="运行中" sx={{ width: 6, height: 6, borderRadius: "50%", bgcolor: "success.main", flexShrink: 0, mr: 0.75 }} />
                    )}
                    <ListItemText
                      primary={taskLabel(t)}
                      primaryTypographyProps={{ noWrap: true, sx: LABEL_ROW }}
                      secondary={
                        taskProjects[t] && taskProjects[t] !== "default"
                          ? displayNameOf(taskProjects[t])
                          : undefined
                      }
                      secondaryTypographyProps={{
                        noWrap: true, variant: "caption", sx: LABEL_META,
                      }}
                    />
                  </ListItemButton>
                  {taskTimes[t] && (
                    <Typography
                      variant="caption"
                      title={new Date(taskTimes[t]).toLocaleString()}
                      sx={{ flexShrink: 0, mr: 1, ...LABEL_META, color: "text.secondary", userSelect: "none" }}
                    >
                      {relTime(taskTimes[t])}
                    </Typography>
                  )}
                </Box>
              ))
            )}
          </List>
        </Collapse>

        {/* 项目右键菜单：新建会话直接挂到该项目下 + 删除项目（C5） */}
        <Menu
          open={projMenuPos !== null}
          onClose={() => setProjMenuPos(null)}
          anchorReference="anchorPosition"
          anchorPosition={projMenuPos ? { top: projMenuPos.mouseY, left: projMenuPos.mouseX } : undefined}
        >
          <MenuItem onClick={async () => {
            const pid = projMenuId;
            setProjMenuPos(null);
            try {
              const r = await taskApi.create({ objective: "", project_id: pid });
              const tid = (r.data as { task?: { task_id?: string } })?.task?.task_id;
              if (!tid) throw new Error("后端未返回 task_id");
              await refreshTasks();
              selectTask(tid);
              setView("chat");
            } catch (e: unknown) {
              const err = e as { response?: { data?: { error?: string } }; message?: string };
              window.alert(`新建会话失败：${err?.response?.data?.error || err?.message || e}`);
            }
          }}>
            <ListItemIcon sx={{ minWidth: 32 }}><PlusIcon fontSize="small" /></ListItemIcon>
            新建会话
          </MenuItem>
          <MenuItem onClick={() => {
            const pid = projMenuId;
            setProjRenameId(pid);
            setProjRenameValue(displayNameOf(pid));
            setProjRenameOpen(true);
            setProjMenuPos(null);
          }}>
            <ListItemIcon sx={{ minWidth: 32 }}><PencilIcon fontSize="small" /></ListItemIcon>
            重命名
          </MenuItem>
          <MenuItem onClick={() => {
            const pid = projMenuId;
            setProjMenuPos(null);
            void deleteProject(pid);
          }} sx={{ color: "error.main" }}>
            <ListItemIcon sx={{ minWidth: 32, color: "error.main" }}><TrashIcon fontSize="small" /></ListItemIcon>
            删除项目
          </MenuItem>
        </Menu>

        {/* 任务右键菜单 */}
        <Menu
          open={menuPos !== null}
          onClose={() => setMenuPos(null)}
          anchorReference="anchorPosition"
          anchorPosition={menuPos ? { top: menuPos.mouseY, left: menuPos.mouseX } : undefined}
        >
          <MenuItem onClick={() => {
            setRenameValue(taskLabel(menuTaskId));
            setRenameOpen(true);
            setMenuPos(null);
          }}>
            <ListItemIcon sx={{ minWidth: 32 }}><PencilIcon fontSize="small" /></ListItemIcon>
            改名
          </MenuItem>
          <MenuItem onClick={async () => {
            const id = menuTaskId;
            setMenuPos(null);
            try {
              const r = await taskApi.openFolder(id);
              if (!r.data?.ok) window.alert(`打开失败：${r.data?.error || "未知错误"}`);
            } catch (e: unknown) {
              const err = e as { response?: { data?: { error?: string } }; message?: string };
              window.alert(`打开失败：${err?.response?.data?.error || err?.message || e}`);
            }
          }}>
            <ListItemIcon sx={{ minWidth: 32 }}><FolderOpenIcon fontSize="small" /></ListItemIcon>
            打开文件夹
          </MenuItem>
          {/* 保存到项目：仅未归属用户项目的任务显示（已在项目里的任务无需再保存） */}
          {!userProjectIds.has(taskProjects[menuTaskId] || "") && (
            <MenuItem onClick={() => {
              setSaveNewProject("");
              setSaveOpen(true);
              setMenuPos(null);
            }}>
              <ListItemIcon sx={{ minWidth: 32 }}><SaveIcon fontSize="small" /></ListItemIcon>
              保存到项目…
            </MenuItem>
          )}
          <MenuItem onClick={() => {
            const id = menuTaskId;
            setMenuPos(null);
            if (window.confirm(`确定删除任务「${taskLabel(id)}」？\n将同时删除其全部落盘数据（轨迹/世界模型/收集/技能）。`)) {
              deleteTask(id);
            }
          }} sx={{ color: "error.main" }}>
            <ListItemIcon sx={{ minWidth: 32, color: "error.main" }}><TrashIcon fontSize="small" /></ListItemIcon>
            删除
          </MenuItem>
        </Menu>

        {/* 重命名对话框 */}
        <Dialog open={renameOpen} onClose={() => setRenameOpen(false)} maxWidth="xs" fullWidth>
          <DialogTitle>重命名任务</DialogTitle>
          <DialogContent>
            <TextField
              autoFocus
              fullWidth
              margin="dense"
              label="任务名"
              value={renameValue}
              onChange={(e) => setRenameValue(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  renameTask(menuTaskId, renameValue);
                  setRenameOpen(false);
                }
              }}
            />
          </DialogContent>
          <DialogActions>
            <Button onClick={() => setRenameOpen(false)}>取消</Button>
            <Button onClick={() => { renameTask(menuTaskId, renameValue); setRenameOpen(false); }}>保存</Button>
          </DialogActions>
        </Dialog>

        {/* 项目重命名对话框（C3）：只改显示名，目录 slug 不变 */}
        <Dialog open={projRenameOpen} onClose={() => setProjRenameOpen(false)} maxWidth="xs" fullWidth>
          <DialogTitle>重命名项目</DialogTitle>
          <DialogContent>
            <TextField
              autoFocus
              margin="dense"
              label="项目名"
              value={projRenameValue}
              onChange={(e) => setProjRenameValue(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") void renameProject();
              }}
              helperText="只改显示名；目录 id 与任务归属不受影响"
              fullWidth
            />
          </DialogContent>
          <DialogActions>
            <Button onClick={() => setProjRenameOpen(false)}>取消</Button>
            <Button onClick={() => void renameProject()}>保存</Button>
          </DialogActions>
        </Dialog>

        {/* 保存到项目对话框（C4）：点选已有项目，或输入显示名新建后保存 */}
        <Dialog open={saveOpen} onClose={() => setSaveOpen(false)} maxWidth="xs" fullWidth>
          <DialogTitle>保存到项目</DialogTitle>
          <DialogContent>
            <List dense disablePadding sx={{ mb: 1 }}>
              {userProjects.map((p) => (
                <ListItemButton
                  key={p.id}
                  onClick={() => { void saveToProject(p.id); }}
                  sx={{ borderRadius: 1, py: 0.5 }}
                >
                  <ListItemIcon sx={{ minWidth: 28 }}><FolderIcon fontSize="small" /></ListItemIcon>
                  <ListItemText
                    primary={p.display_name || p.id}
                    primaryTypographyProps={{ style: { fontSize: 13 } }}
                  />
                </ListItemButton>
              ))}
            </List>
            <TextField
              fullWidth
              margin="dense"
              label="或新建项目（显示名，可中文）"
              size="small"
              value={saveNewProject}
              onChange={(e) => setSaveNewProject(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") createAndSave(); }}
            />
          </DialogContent>
          <DialogActions>
            <Button onClick={() => setSaveOpen(false)}>取消</Button>
            <Button onClick={createAndSave} disabled={!saveNewProject.trim()}>新建并保存</Button>
          </DialogActions>
        </Dialog>

        </Box>

        {/* 底部固定：设置入口（距底 20px） */}
        <Box sx={{ flexShrink: 0, px: 1, pb: 2.5 }}>
          {overlayEntry(settingsOpen, "设置", <SettingsIcon />, () => setSettingsOpen(true))}
        </Box>
      </Drawer>

      {/* 主区（全屏，无内边距，由页面自身控制间距） */}
      <Box component="main" sx={{ flexGrow: 1, minWidth: 0, minHeight: 0, overflow: "auto", height: "100%", p: 0 }}>
        {renderMain()}
      </Box>

      {/* 关设置时再发一次目录变更信号：兜住「改了但没走保存路径」的入口（如导入/手工编辑） */}
      {settingsOpen && (
        <Settings onClose={() => { setSettingsOpen(false); notifyModelsChanged(); }} />
      )}
      {skillsOpen && <SkillsAndTools onClose={() => setSkillsOpen(false)} />}
    </Box>
  );
}
