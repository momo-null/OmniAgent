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
import AddIcon from "@mui/icons-material/Add";
import BuildIcon from "@mui/icons-material/Build";
import SettingsIcon from "@mui/icons-material/Settings";
import ExpandLess from "@mui/icons-material/ExpandLess";
import ExpandMore from "@mui/icons-material/ExpandMore";
import ScheduleIcon from "@mui/icons-material/Schedule";
import MoreVertIcon from "@mui/icons-material/MoreVert";
import EditIcon from "@mui/icons-material/Edit";
import DeleteIcon from "@mui/icons-material/Delete";
import FolderOpenIcon from "@mui/icons-material/FolderOpen";
import { taskApi, projectApi } from "../api/client";
import SaveIcon from "@mui/icons-material/Save";
import FolderIcon from "@mui/icons-material/Folder";
import { useTaskStore, type MainView } from "../store/taskStore.tsx";
import Chat from "../pages/Chat";
import SkillsAndTools from "../pages/SkillsAndTools";
import Settings from "../pages/Settings";

const DRAWER_WIDTH = 260;

export default function Layout() {
  const {
    view, setView, tasks, currentTaskId, selectTask,
    refreshTasks, connectStream, disconnectStream, taskTitles, taskObjectives,
    taskProjects, deleteTask, renameTask,
  } = useTaskStore();

  const [tasksOpen, setTasksOpen] = useState(true);

  // 知识分层 C4/C5：项目侧栏 + 保存到项目
  const [projects, setProjects] = useState<{ id: string; display_name: string; session_count: number }[]>([]);
  const [projectsOpen, setProjectsOpen] = useState(true);
  // 项目行的展开态（缺省展开）：保存到项目的任务嵌套挂在项目下
  const [openProjects, setOpenProjects] = useState<Record<string, boolean>>({});
  const [saveOpen, setSaveOpen] = useState(false);
  const [saveNewProject, setSaveNewProject] = useState("");
  const [projMenuAnchor, setProjMenuAnchor] = useState<null | HTMLElement>(null);
  const [projMenuId, setProjMenuId] = useState("");

  const refreshProjects = () =>
    projectApi.meta()
      .then((r) => setProjects(r.data?.projects || []))
      .catch(() => { /* 忽略瞬时错误 */ });

  useEffect(() => { refreshProjects(); }, []);

  // 任务右键菜单状态
  const [menuAnchor, setMenuAnchor] = useState<null | HTMLElement>(null);
  const [menuTaskId, setMenuTaskId] = useState("");
  // 重命名对话框状态
  const [renameOpen, setRenameOpen] = useState(false);
  const [renameValue, setRenameValue] = useState("");

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

  const renderMain = () => {
    switch (view) {
      case "skills": return <SkillsAndTools />;
      case "settings": return <Settings />;
      case "chat":
      default: return <Chat />;
    }
  };

  const sidebarEntry = (v: MainView, label: string, icon: React.ReactNode) => (
    <ListItemButton
      selected={view === v}
      onClick={() => setView(v)}
      sx={{ borderRadius: 1, py: 0.5 }}
    >
      <ListItemIcon sx={{ minWidth: 32 }}>{icon}</ListItemIcon>
      <ListItemText primary={label} primaryTypographyProps={{ fontSize: 14 }} />
    </ListItemButton>
  );

  return (
    <Box sx={{ display: "flex", height: "100vh", width: "100%", overflow: "hidden" }}>
      <Drawer
        variant="permanent"
        sx={{
          width: DRAWER_WIDTH, flexShrink: 0,
          "& .MuiDrawer-paper": {
            width: DRAWER_WIDTH, boxSizing: "border-box", overflowY: "auto",
            // 侧栏比内容区亮一档但偏暗（#2e2e2e），层级靠亮度区分
            borderRight: "none", bgcolor: "#2e2e2e",
          },
        }}
      >
        <Toolbar>
          <Typography variant="subtitle1" noWrap sx={{ fontWeight: 600 }}>OmniAgent</Typography>
        </Toolbar>

        {/* 固定入口 */}
        <List sx={{ py: 0 }}>
          <ListItemButton
            onClick={() => { selectTask(""); setView("chat"); }}
            sx={{ borderRadius: 1, py: 0.5 }}
          >
            <ListItemIcon sx={{ minWidth: 32 }}><AddIcon /></ListItemIcon>
            <ListItemText primary="新建对话" primaryTypographyProps={{ fontSize: 14 }} />
          </ListItemButton>
          {sidebarEntry("skills", "技能与工具", <BuildIcon />)}
        </List>

        {/* 项目分区（C4）：用户创建的项目（default 为内核缺省归属，不展示）+ 会话数；菜单支持删除（C5） */}
        <Divider sx={{ mt: 2, mb: 0.5, borderColor: "divider" }} />
        <Box
          sx={{
            display: "flex", alignItems: "center", justifyContent: "space-between",
            px: 2, py: 0.5,
          }}
        >
          <Typography variant="body2" color="text.secondary" sx={{ fontWeight: 600 }}>
            项目
          </Typography>
          <IconButton size="small" onClick={() => setProjectsOpen((o) => !o)} sx={{ p: 0.25 }}>
            {projectsOpen ? <ExpandLess fontSize="small" /> : <ExpandMore fontSize="small" />}
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
                const open = openProjects[p.id] ?? true;
                return (
                  <Box key={p.id}>
                    <Box
                      sx={{ display: "flex", alignItems: "center", borderRadius: 1, "&:hover": { bgcolor: "action.hover" } }}
                    >
                      <ListItemButton
                        onClick={() => setOpenProjects((prev) => ({ ...prev, [p.id]: !open }))}
                        sx={{ borderRadius: 1, py: 0.25, flexGrow: 1, minWidth: 0 }}
                      >
                        <ListItemIcon sx={{ minWidth: 28 }}>
                          <FolderIcon fontSize="small" />
                        </ListItemIcon>
                        <ListItemText
                          primary={p.display_name || p.id}
                          primaryTypographyProps={{ noWrap: true, style: { fontSize: 13 } }}
                          secondary={`${projTasks.length} 任务`}
                          secondaryTypographyProps={{ noWrap: true, variant: "caption", sx: { fontSize: 11 } }}
                        />
                      </ListItemButton>
                      <IconButton
                        size="small"
                        sx={{ flexShrink: 0, ml: 0.25 }}
                        onClick={(e) => {
                          e.stopPropagation();
                          setProjMenuId(p.id);
                          setProjMenuAnchor(e.currentTarget);
                        }}
                        aria-label="项目操作"
                      >
                        <MoreVertIcon fontSize="small" />
                      </IconButton>
                    </Box>
                    {/* 项目下的任务（保存到项目的任务挂在这里，点击打开会话） */}
                    {projTasks.length > 0 && (
                      <Collapse in={open} timeout="auto" unmountOnExit>
                        <List dense disablePadding sx={{ pl: 3.5, pb: 0.5 }}>
                          {projTasks.map((t) => (
                            <Box
                              key={t}
                              sx={{ display: "flex", alignItems: "center", borderRadius: 1, "&:hover": { bgcolor: "action.hover" } }}
                            >
                              <ListItemButton
                                selected={t === currentTaskId}
                                onClick={() => { selectTask(t); setView("chat"); }}
                                sx={{ borderRadius: 1, py: 0.25, flexGrow: 1, minWidth: 0 }}
                              >
                                <ListItemText
                                  primary={taskLabel(t)}
                                  primaryTypographyProps={{ noWrap: true, style: { fontSize: 12.5 } }}
                                />
                              </ListItemButton>
                              <IconButton
                                size="small"
                                sx={{ flexShrink: 0, ml: 0.25 }}
                                onClick={(e) => {
                                  e.stopPropagation();
                                  setMenuTaskId(t);
                                  setMenuAnchor(e.currentTarget);
                                }}
                                aria-label="任务操作"
                              >
                                <MoreVertIcon fontSize="small" />
                              </IconButton>
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
          <Typography variant="body2" color="text.secondary" sx={{ fontWeight: 600 }}>
            任务
          </Typography>
          <IconButton size="small" onClick={() => setTasksOpen((o) => !o)} sx={{ p: 0.25 }}>
            {tasksOpen ? <ExpandLess fontSize="small" /> : <ExpandMore fontSize="small" />}
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
                  sx={{ display: "flex", alignItems: "center", borderRadius: 1, "&:hover": { bgcolor: "action.hover" } }}
                >
                  <ListItemButton
                    selected={t === currentTaskId}
                    onClick={() => { selectTask(t); setView("chat"); }}
                    sx={{ borderRadius: 1, py: 0.25, flexGrow: 1, minWidth: 0 }}
                  >
                    <ListItemText
                      primary={taskLabel(t)}
                      primaryTypographyProps={{ noWrap: true, style: { fontSize: 13 } }}
                      secondary={
                        taskProjects[t] && taskProjects[t] !== "default"
                          ? displayNameOf(taskProjects[t])
                          : undefined
                      }
                      secondaryTypographyProps={{
                        noWrap: true, variant: "caption", sx: { fontSize: 11 },
                      }}
                    />
                  </ListItemButton>
                  <IconButton
                    size="small"
                    sx={{ flexShrink: 0, ml: 0.25 }}
                    onClick={(e) => {
                      e.stopPropagation();
                      setMenuTaskId(t);
                      setMenuAnchor(e.currentTarget);
                    }}
                    aria-label="任务操作"
                  >
                    <MoreVertIcon fontSize="small" />
                  </IconButton>
                </Box>
              ))
            )}
          </List>
        </Collapse>

        {/* 项目操作菜单：新建会话直接挂到该项目下 + 删除项目（C5） */}
        <Menu
          anchorEl={projMenuAnchor}
          open={Boolean(projMenuAnchor)}
          onClose={() => setProjMenuAnchor(null)}
        >
          <MenuItem onClick={async () => {
            const pid = projMenuId;
            setProjMenuAnchor(null);
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
            <ListItemIcon sx={{ minWidth: 32 }}><AddIcon fontSize="small" /></ListItemIcon>
            新建会话
          </MenuItem>
          <MenuItem onClick={() => {
            const pid = projMenuId;
            setProjMenuAnchor(null);
            void deleteProject(pid);
          }} sx={{ color: "error.main" }}>
            <ListItemIcon sx={{ minWidth: 32, color: "error.main" }}><DeleteIcon fontSize="small" /></ListItemIcon>
            删除项目
          </MenuItem>
        </Menu>

        {/* 任务右键/更多菜单 */}
        <Menu
          anchorEl={menuAnchor}
          open={Boolean(menuAnchor)}
          onClose={() => setMenuAnchor(null)}
        >
          <MenuItem onClick={() => {
            setRenameValue(taskLabel(menuTaskId));
            setRenameOpen(true);
            setMenuAnchor(null);
          }}>
            <ListItemIcon sx={{ minWidth: 32 }}><EditIcon fontSize="small" /></ListItemIcon>
            改名
          </MenuItem>
          <MenuItem onClick={async () => {
            const id = menuTaskId;
            setMenuAnchor(null);
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
              setMenuAnchor(null);
            }}>
              <ListItemIcon sx={{ minWidth: 32 }}><SaveIcon fontSize="small" /></ListItemIcon>
              保存到项目…
            </MenuItem>
          )}
          <MenuItem onClick={() => {
            const id = menuTaskId;
            setMenuAnchor(null);
            if (window.confirm(`确定删除任务「${taskLabel(id)}」？\n将同时删除其全部落盘数据（轨迹/世界模型/收集/技能）。`)) {
              deleteTask(id);
            }
          }} sx={{ color: "error.main" }}>
            <ListItemIcon sx={{ minWidth: 32, color: "error.main" }}><DeleteIcon fontSize="small" /></ListItemIcon>
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

        {/* 定时任务占位 */}
        <ListItemButton disabled sx={{ py: 0.5 }}>
          <ListItemIcon sx={{ minWidth: 32 }}><ScheduleIcon /></ListItemIcon>
          <ListItemText primary="定时任务（规划中）" primaryTypographyProps={{ fontSize: 13 }} />
        </ListItemButton>

        {/* 隐藏分区：淡分隔线 + 正常大小设置入口（仅文字可点） */}
        <Divider sx={{ my: 0.25, borderColor: "divider" }} />
        <Box
          sx={{
            display: "flex", alignItems: "center", gap: 1,
            px: 2, py: 0.5,
          }}
        >
          <SettingsIcon fontSize="small" />
          <Typography
            variant="body2"
            color="text.secondary"
            sx={{ fontWeight: 600, cursor: "pointer", "&:hover": { color: "primary.main" } }}
            onClick={() => setView("settings")}
          >
            设置
          </Typography>
        </Box>
      </Drawer>

      {/* 主区（全屏，无内边距，由页面自身控制间距） */}
      <Box component="main" sx={{ flexGrow: 1, minWidth: 0, minHeight: 0, overflow: "auto", height: "100%", p: 0 }}>
        {renderMain()}
      </Box>
    </Box>
  );
}
