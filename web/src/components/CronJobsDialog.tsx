// 定时任务管理对话框（点亮 Layout 预留位；数据源 ~/.omniagent/cron/jobs.json 热加载）
import { useEffect, useState } from "react";
import {
  Alert, Box, Button, Checkbox, Dialog, DialogActions, DialogContent, DialogTitle,
  FormControlLabel, IconButton, List, ListItem, ListItemText, MenuItem, Snackbar,
  Switch, TextField, Tooltip, Typography,
} from "@mui/material";
import AddIcon from "@mui/icons-material/Add";
import DeleteIcon from "@mui/icons-material/Delete";
import PlayArrowIcon from "@mui/icons-material/PlayArrow";
import EditIcon from "@mui/icons-material/Edit";
import { cronApi } from "../api/client";

export interface CronJob {
  id: string;
  name?: string;
  cron: string;
  objective: string;
  done_when?: string;
  max_steps?: number;
  project_id?: string;
  notify_channel?: string;
  enabled: boolean;
  miss_grace_minutes?: number;
  note?: string;
}

const EMPTY_FORM: CronJob = {
  id: "", name: "", cron: "0 9 * * 1-5", objective: "", done_when: "",
  max_steps: 40, project_id: "default", notify_channel: "feishu", enabled: false,
  miss_grace_minutes: 60, note: "",
};

export default function CronJobsDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [jobs, setJobs] = useState<CronJob[]>([]);
  const [projects, setProjects] = useState<string[]>(["default"]);
  const [error, setError] = useState("");
  const [toast, setToast] = useState("");
  const [editing, setEditing] = useState<CronJob | null>(null);

  const load = async () => {
    try {
      const [j, p] = await Promise.all([cronApi.list(), cronApi.projects()]);
      setJobs(j.data.jobs ?? []);
      setProjects(p.data.projects ?? ["default"]);
      setError("");
    } catch (e: unknown) {
      setError(String((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? e));
    }
  };

  useEffect(() => { if (open) void load(); }, [open]);

  const persist = async (next: CronJob[]) => {
    try {
      await cronApi.save(next as unknown as Record<string, unknown>[]);
      setJobs(next);
      setError("");
    } catch (e: unknown) {
      setError(String((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? e));
      void load();
    }
  };

  const toggle = (id: string, enabled: boolean) =>
    void persist(jobs.map(j => (j.id === id ? { ...j, enabled } : j)));

  const remove = (id: string) => void persist(jobs.filter(j => j.id !== id));

  const runNow = async (id: string) => {
    try {
      const r = await cronApi.run(id);
      setToast(`已派发（task ${String(r.data.task_id).slice(0, 8)}），结果将按通知通道推送`);
    } catch (e: unknown) {
      setToast(`运行失败: ${String(e)}`);
    }
  };

  const saveForm = async () => {
    if (!editing) return;
    const exists = jobs.some(j => j.id === editing.id);
    const next = exists ? jobs.map(j => (j.id === editing.id ? editing : j)) : [...jobs, editing];
    await persist(next);
    setEditing(null);
  };

  return (
    <>
      <Dialog open={open} onClose={onClose} maxWidth="sm" fullWidth>
        <DialogTitle sx={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          <span>定时任务</span>
          <Button size="small" startIcon={<AddIcon />} onClick={() => setEditing({ ...EMPTY_FORM })}>新建</Button>
        </DialogTitle>
        <DialogContent dividers>
          {error && <Alert severity="error" sx={{ mb: 1 }}>{error}</Alert>}
          <List dense disablePadding>
            {jobs.map(j => (
              <ListItem key={j.id} secondaryAction={
                <Box sx={{ display: "flex", gap: 0.5, alignItems: "center" }}>
                  <Tooltip title="立即运行"><IconButton size="small" onClick={() => void runNow(j.id)}><PlayArrowIcon fontSize="small" /></IconButton></Tooltip>
                  <Tooltip title="编辑"><IconButton size="small" onClick={() => setEditing({ ...j })}><EditIcon fontSize="small" /></IconButton></Tooltip>
                  <Tooltip title="删除"><IconButton size="small" onClick={() => remove(j.id)}><DeleteIcon fontSize="small" /></IconButton></Tooltip>
                  <Switch size="small" checked={!!j.enabled} onChange={e => toggle(j.id, e.target.checked)} />
                </Box>
              }>
                <ListItemText
                  primary={<>{j.name || j.id} <Typography component="span" variant="caption" color="text.secondary">{j.cron}</Typography></>}
                  secondary={`${j.objective.slice(0, 50)}${j.objective.length > 50 ? "…" : ""}｜项目: ${j.project_id || "default"}｜通知: ${j.notify_channel || "无"}`}
                  primaryTypographyProps={{ fontSize: 13 }}
                  secondaryTypographyProps={{ fontSize: 11, noWrap: true }}
                />
              </ListItem>
            ))}
            {!jobs.length && <Typography variant="body2" color="text.secondary" sx={{ p: 2 }}>暂无任务，点「新建」添加</Typography>}
          </List>
        </DialogContent>
        <DialogActions><Button onClick={onClose}>关闭</Button></DialogActions>
      </Dialog>

      {/* 新建/编辑 */}
      <Dialog open={!!editing} onClose={() => setEditing(null)} maxWidth="xs" fullWidth>
        <DialogTitle>{editing && jobs.some(j => j.id === editing.id) ? "编辑任务" : "新建任务"}</DialogTitle>
        {editing && (
          <DialogContent sx={{ display: "flex", flexDirection: "column", gap: 1.5, pt: 1 }}>
            <TextField size="small" label="ID（英文唯一）" value={editing.id}
              onChange={e => setEditing({ ...editing, id: e.target.value.trim() })} />
            <TextField size="small" label="名称" value={editing.name ?? ""}
              onChange={e => setEditing({ ...editing, name: e.target.value })} />
            <TextField size="small" label="cron（分 时 日 月 周）" value={editing.cron}
              onChange={e => setEditing({ ...editing, cron: e.target.value.trim() })} />
            <TextField size="small" multiline minRows={2} label="objective（一句话任务目标；禁止路径/脚本名）"
              value={editing.objective} onChange={e => setEditing({ ...editing, objective: e.target.value })} />
            <TextField size="small" label="done_when（完成判据）" value={editing.done_when ?? ""}
              onChange={e => setEditing({ ...editing, done_when: e.target.value })} />
            <TextField size="small" select label="关联项目（AGENTS.md 注入归属）" value={editing.project_id ?? "default"}
              onChange={e => setEditing({ ...editing, project_id: e.target.value })}>
              {projects.map(p => <MenuItem key={p} value={p}>{p}</MenuItem>)}
            </TextField>
            <TextField size="small" select label="结果通知通道" value={editing.notify_channel ?? ""}
              onChange={e => setEditing({ ...editing, notify_channel: e.target.value })}>
              <MenuItem value="">不推送</MenuItem>
              <MenuItem value="feishu">feishu</MenuItem>
              <MenuItem value="wecom">wecom（P1）</MenuItem>
            </TextField>
            <TextField size="small" type="number" label="max_steps" value={editing.max_steps ?? 40}
              onChange={e => setEditing({ ...editing, max_steps: Number(e.target.value) || 40 })} />
            <TextField size="small" type="number" label="错过补偿窗口（分钟）" value={editing.miss_grace_minutes ?? 60}
              onChange={e => setEditing({ ...editing, miss_grace_minutes: Number(e.target.value) || 60 })} />
            <FormControlLabel label="启用" control={<Checkbox checked={!!editing.enabled}
              onChange={e => setEditing({ ...editing, enabled: e.target.checked })} />} />
          </DialogContent>
        )}
        <DialogActions>
          <Button onClick={() => setEditing(null)}>取消</Button>
          <Button onClick={() => void saveForm()} disabled={!editing?.id || !editing?.objective || !editing?.cron}>保存</Button>
        </DialogActions>
      </Dialog>

      <Snackbar open={!!toast} autoHideDuration={4000} onClose={() => setToast("")} message={toast} />
    </>
  );
}
