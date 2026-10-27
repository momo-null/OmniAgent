// 定时任务页（接入设计 v1.1 §5 前端）：主列表 + 点入详情（运行记录时间线）
// 数据源：GET/PUT /api/cron/jobs（~/.omniagent/cron/jobs.json 热加载）+ GET /api/cron/runs
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert, Box, Button, Checkbox, Chip, Dialog, DialogActions, DialogContent, DialogTitle,
  FormControlLabel, IconButton, InputAdornment, MenuItem, Paper, Snackbar, Switch,
  TextField, Tooltip, Typography,
} from "@mui/material";
import AddOutlinedIcon from "@mui/icons-material/AddOutlined";
import ArrowBackOutlinedIcon from "@mui/icons-material/ArrowBackOutlined";
import DeleteOutlinedIcon from "@mui/icons-material/DeleteOutlined";
import EditOutlinedIcon from "@mui/icons-material/EditOutlined";
import PlayArrowOutlinedIcon from "@mui/icons-material/PlayArrowOutlined";
import SearchOutlinedIcon from "@mui/icons-material/SearchOutlined";
import ScheduleOutlinedIcon from "@mui/icons-material/ScheduleOutlined";
import { cronApi } from "../../api/client";

interface CronJob {
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
  describe?: string;        // 后端派生：cron 人话
  next_run_at?: string | null;
  next_in_seconds?: number | null;
}

interface RunRec {
  ts: string;
  job_id: string;
  task_id: string;
  status: string;
  summary?: string;
  error?: string;
}

const EMPTY_FORM: CronJob = {
  id: "", name: "", cron: "0 9 * * 1-5", objective: "", done_when: "",
  max_steps: 40, project_id: "default", notify_channel: "feishu", enabled: false,
  miss_grace_minutes: 60, note: "",
};

const fmtCountdown = (sec?: number | null): string => {
  if (sec == null) return "";
  if (sec < 60) return `${sec} 秒后执行`;
  if (sec < 3600) return `${Math.round(sec / 60)} 分钟后执行`;
  const h = Math.floor(sec / 3600), m = Math.round((sec % 3600) / 60);
  return m ? `${h} 小时 ${m} 分钟后执行` : `${h} 小时后执行`;
};

const fmtTs = (iso: string): string => {
  const d = new Date(iso);
  if (isNaN(+d)) return iso;
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  const hh = `${d.getHours()}`.padStart(2, "0"), mm = `${d.getMinutes()}`.padStart(2, "0");
  return sameDay ? `今天 ${d.getMonth() + 1}月${d.getDate()}日 ${hh}:${mm}`
    : `${d.getMonth() + 1}月${d.getDate()}日 ${hh}:${mm}`;
};

export default function CronJobs() {
  const [tab, setTab] = useState<"jobs" | "runs">("jobs");
  const [jobs, setJobs] = useState<CronJob[]>([]);
  const [projects, setProjects] = useState<string[]>(["default"]);
  const [filter, setFilter] = useState<"all" | "on" | "off">("all");
  const [search, setSearch] = useState("");
  const [detail, setDetail] = useState<string | null>(null);   // job_id → 详情视图
  const [runs, setRuns] = useState<RunRec[]>([]);
  const [editing, setEditing] = useState<CronJob | null>(null);
  const [error, setError] = useState("");
  const [toast, setToast] = useState("");

  const loadJobs = useCallback(async () => {
    try {
      const r = await cronApi.list();
      setJobs(r.data.jobs ?? []);
      setError("");
    } catch (e: unknown) {
      setError(String((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? e));
    }
  }, []);

  useEffect(() => { void loadJobs(); const t = setInterval(() => void loadJobs(), 60_000); return () => clearInterval(t); }, [loadJobs]);
  useEffect(() => { void (async () => { try { const p = await cronApi.projects(); setProjects(p.data.projects ?? ["default"]); } catch { /* 忽略 */ } })(); }, []);

  const loadRuns = useCallback(async (jobId: string) => {
    try {
      const r = await cronApi.runs(jobId, 100);
      setRuns(r.data.runs ?? []);
    } catch { setRuns([]); }
  }, []);
  useEffect(() => { if (detail) void loadRuns(detail); }, [detail, loadRuns]);
  // 执行中的任务：详情页每 5s 轮询，跑完自动出现结果
  useEffect(() => {
    if (!detail) return;
    const t = setInterval(() => void loadRuns(detail), 5000);
    return () => clearInterval(t);
  }, [detail, loadRuns]);

  const persist = async (next: CronJob[]) => {
    try {
      await cronApi.save(next as unknown as Record<string, unknown>[]);
      setJobs((await cronApi.list()).data.jobs ?? []);
      setError("");
    } catch (e: unknown) {
      setError(String((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? e));
    }
  };

  const runNow = async (id: string) => {
    try {
      const r = await cronApi.run(id);
      setToast(`已派发（task ${String(r.data.task_id).slice(0, 8)}），结果将按通知通道推送`);
      setTimeout(() => detail === id && void loadRuns(id), 3000);
    } catch (e: unknown) {
      setToast(`运行失败: ${String(e)}`);
    }
  };

  const saveForm = async () => {
    if (!editing) return;
    const exists = jobs.some(j => j.id === editing.id);
    await persist(exists ? jobs.map(j => (j.id === editing.id ? editing : j)) : [...jobs, editing]);
    setEditing(null);
  };

  const counts = useMemo(() => ({
    all: jobs.length,
    on: jobs.filter(j => j.enabled).length,
    off: jobs.filter(j => !j.enabled).length,
  }), [jobs]);

  const visible = useMemo(() => jobs.filter(j =>
    (filter === "all" || (filter === "on") === j.enabled)
    && (!search || `${j.name ?? ""}${j.id}${j.objective}`.toLowerCase().includes(search.toLowerCase()))
  ), [jobs, filter, search]);

  const detailJob = jobs.find(j => j.id === detail);

  // ── 详情视图（截图2：标题行 + 运行记录时间线 + 下一次运行） ──
  if (detail && detailJob) {
    return (
      <Box sx={{ height: "100%", overflowY: "auto", p: { xs: 2, md: 3 } }}>
        <Box sx={{ display: "flex", alignItems: "center", gap: 1, mb: 2 }}>
          <IconButton size="small" onClick={() => setDetail(null)}><ArrowBackOutlinedIcon /></IconButton>
          <Typography variant="h6" sx={{ fontSize: 18 }}>{detailJob.name || detailJob.id}</Typography>
          <Typography variant="body2" color="text.secondary">{detailJob.describe ?? detailJob.cron}</Typography>
          <Box sx={{ flex: 1 }} />
          <Switch checked={!!detailJob.enabled} onChange={e => void persist(jobs.map(j => j.id === detailJob.id ? { ...j, enabled: e.target.checked } : j))} />
          <Tooltip title="立即运行"><IconButton onClick={() => void runNow(detailJob.id)}><PlayArrowOutlinedIcon /></IconButton></Tooltip>
          <Tooltip title="编辑"><IconButton onClick={() => setEditing({ ...detailJob })}><EditOutlinedIcon /></IconButton></Tooltip>
        </Box>
        {detailJob.next_run_at && (
          <Typography color="text.secondary" sx={{ mb: 2, fontSize: 13 }}>
            {fmtTs(detailJob.next_run_at)} 下一次运行（{fmtCountdown(detailJob.next_in_seconds)}）
          </Typography>
        )}
        {!runs.length && <Typography color="text.secondary" sx={{ mt: 6, textAlign: "center" }}>暂无运行记录</Typography>}
        {runs.map((r, i) => (
          <Box key={`${r.ts}-${i}`} sx={{ mb: 3 }}>
            <Typography sx={{ fontSize: 15, fontWeight: 600, mb: 1 }}>{fmtTs(r.ts)}</Typography>
            <Paper variant="outlined" sx={{ p: 2, bgcolor: "background.default" }}>
              {r.status === "running" ? (
                <Typography sx={{ color: "warning.main", fontSize: 14 }}>⏳ 任务执行中…（完成后此条自动更新）</Typography>
              ) : r.status === "error" ? (
                <Typography sx={{ color: "error.main", fontSize: 14 }}>ⓘ {r.error || "任务执行失败"}</Typography>
              ) : (
                <Typography sx={{ whiteSpace: "pre-wrap", fontSize: 14, lineHeight: 1.7 }}>{r.summary || "（无摘要）"}</Typography>
              )}
            </Paper>
          </Box>
        ))}
      </Box>
    );
  }

  // ── 主列表视图（截图1：页签 + 筛选 chips + 卡片 + 新建） ──
  return (
    <Box sx={{ height: "100%", overflowY: "auto", p: { xs: 2, md: 3 } }}>
      <Box sx={{ display: "flex", alignItems: "center", gap: 2, mb: 1 }}>
        {/* 页签对：两个标题都可点，激活项高亮（修复：点过运行记录后切不回定时任务列表） */}
        <Typography onClick={() => setTab("jobs")}
          sx={{ fontSize: 20, fontWeight: 700, cursor: "pointer",
                color: tab === "jobs" ? "text.primary" : "text.secondary",
                "&:hover": { color: "primary.main" } }}>定时任务</Typography>
        <Typography onClick={() => setTab("runs")}
          sx={{ fontSize: 20, fontWeight: 700, cursor: "pointer",
                color: tab === "runs" ? "text.primary" : "text.secondary",
                "&:hover": { color: "primary.main" } }}>运行记录</Typography>
        <Box sx={{ flex: 1 }} />
        <IconButton size="small" onClick={() => void loadJobs()}><SearchOutlinedIcon fontSize="small" /></IconButton>
        <Button variant="contained" size="small" startIcon={<AddOutlinedIcon />} onClick={() => setEditing({ ...EMPTY_FORM })}>新建</Button>
      </Box>

      {tab === "jobs" ? (
        <>
          <Box sx={{ display: "flex", gap: 1, mb: 2 }}>
            {(["all", "on", "off"] as const).map(f => (
              <Chip key={f} clickable onClick={() => setFilter(f)}
                label={`${f === "all" ? "全部" : f === "on" ? "已启用" : "已暂停"} (${counts[f]})`}
                color={filter === f ? "primary" : "default"} size="small" variant={filter === f ? "filled" : "outlined"} />
            ))}
            {search && (
              <TextField size="small" autoFocus placeholder="搜索…" value={search}
                onChange={e => setSearch(e.target.value)}
                sx={{ width: 200 }}
                InputProps={{ startAdornment: <InputAdornment position="start"><SearchOutlinedIcon fontSize="small" /></InputAdornment> }} />
            )}
          </Box>

          {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}

          <Box sx={{ display: "grid", gridTemplateColumns: { xs: "1fr", md: "1fr 1fr" }, gap: 2 }}>
            {visible.map(j => (
              <Paper key={j.id} variant="outlined"
                sx={{ p: 2, cursor: "pointer", bgcolor: "background.default", "&:hover": { borderColor: "primary.main" } }}
                onClick={() => setDetail(j.id)}>
                <Box sx={{ display: "flex", alignItems: "center", gap: 1 }}>
                  <Typography sx={{ fontWeight: 700, fontSize: 15, flex: 1 }}
                    onClick={e => { e.stopPropagation(); setEditing({ ...j }); }}>
                    {j.name || j.id}
                  </Typography>
                  <Switch size="small" checked={!!j.enabled}
                    onClick={e => e.stopPropagation()}
                    onChange={e => void persist(jobs.map(x => x.id === j.id ? { ...x, enabled: e.target.checked } : x))} />
                </Box>
                <Typography variant="body2" color="text.secondary"
                  sx={{ mb: 1.5, display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical", overflow: "hidden" }}>
                  {j.objective}
                </Typography>
                <Box sx={{ display: "flex", alignItems: "center", gap: 1 }}>
                  <ScheduleOutlinedIcon sx={{ fontSize: 15, color: "text.secondary" }} />
                  <Typography variant="body2" color="text.secondary" sx={{ flex: 1 }}>
                    {j.describe ?? j.cron}{j.enabled && j.next_run_at ? `　·　${fmtCountdown(j.next_in_seconds)}` : ""}
                  </Typography>
                  <Tooltip title="立即运行">
                    <IconButton size="small" onClick={e => { e.stopPropagation(); void runNow(j.id); }}>
                      <PlayArrowOutlinedIcon fontSize="small" />
                    </IconButton>
                  </Tooltip>
                  <Tooltip title="删除">
                    <IconButton size="small" onClick={e => { e.stopPropagation(); void persist(jobs.filter(x => x.id !== j.id)); }}>
                      <DeleteOutlinedIcon fontSize="small" />
                    </IconButton>
                  </Tooltip>
                </Box>
              </Paper>
            ))}
            {!visible.length && <Typography color="text.secondary" sx={{ gridColumn: "1/-1", textAlign: "center", mt: 8 }}>暂无任务</Typography>}
          </Box>
        </>
      ) : (
        // 运行记录总览（跨 job）
        <AllRuns />
      )}

      {/* 新建/编辑表单（MUI 对话框；保存写 jobs.json 热加载） */}
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
    </Box>
  );
}

// 运行记录总 Tab：全部 job 的运行流水（新→旧）
function AllRuns() {
  const [runs, setRuns] = useState<RunRec[]>([]);
  useEffect(() => { void (async () => { try { const r = await cronApi.runs("", 200); setRuns(r.data.runs ?? []); } catch { setRuns([]); } })(); }, []);
  if (!runs.length) return <Typography color="text.secondary" sx={{ textAlign: "center", mt: 8 }}>暂无运行记录</Typography>;
  return (
    <Box sx={{ mt: 2 }}>
      {runs.map((r, i) => (
        <Box key={`${r.ts}-${i}`} sx={{ mb: 2.5 }}>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 0.5 }}>
            {fmtTs(r.ts)}｜{r.job_id}
          </Typography>
          <Paper variant="outlined" sx={{ p: 1.5, bgcolor: "background.default" }}>
            {r.status === "running" ? (
              <Typography sx={{ color: "warning.main", fontSize: 13 }}>⏳ 执行中…</Typography>
            ) : r.status === "error" ? (
              <Typography sx={{ color: "error.main", fontSize: 13 }}>ⓘ {r.error || "执行失败"}</Typography>
            ) : (
              <Typography sx={{ fontSize: 13, whiteSpace: "pre-wrap" }}>{r.summary || "（无摘要）"}</Typography>
            )}
          </Paper>
        </Box>
      ))}
    </Box>
  );
}
