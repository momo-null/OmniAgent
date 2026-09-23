import { useEffect, useState } from "react";
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
import { useTaskStore, type MainView } from "../store/taskStore.tsx";
import Chat from "../pages/Chat";
import SkillsAndTools from "../pages/SkillsAndTools";
import Settings from "../pages/Settings";

const DRAWER_WIDTH = 200;

export default function Layout() {
  const {
    view, setView, tasks, currentTaskId, selectTask,
    refreshTasks, connectStream, disconnectStream, taskTitles, taskObjectives,
    deleteTask, renameTask,
  } = useTaskStore();

  const [tasksOpen, setTasksOpen] = useState(true);

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
            borderRight: "none", bgcolor: "transparent",
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

        {/* 隐藏分区：淡分隔线 + 正常大小标题（纯文字，非按钮） */}
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
            {tasks.length === 0 ? (
              <ListItemText primary="（暂无任务）" primaryTypographyProps={{ variant: "caption", color: "text.secondary", sx: { pl: 2 } }} />
            ) : (
              tasks.map((t: string) => (
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
                      primary={taskTitles[t] || taskObjectives[t] || t}
                      primaryTypographyProps={{ noWrap: true, style: { fontSize: 13 } }}
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

        {/* 任务右键/更多菜单 */}
        <Menu
          anchorEl={menuAnchor}
          open={Boolean(menuAnchor)}
          onClose={() => setMenuAnchor(null)}
        >
          <MenuItem onClick={() => {
            setRenameValue(taskTitles[menuTaskId] || taskObjectives[menuTaskId] || menuTaskId);
            setRenameOpen(true);
            setMenuAnchor(null);
          }}>
            <ListItemIcon sx={{ minWidth: 32 }}><EditIcon fontSize="small" /></ListItemIcon>
            改名
          </MenuItem>
          <MenuItem onClick={() => {
            const id = menuTaskId;
            setMenuAnchor(null);
            if (window.confirm(`确定删除任务「${taskTitles[id] || id}」？\n将同时删除其全部落盘数据（轨迹/世界模型/收集/技能）。`)) {
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
