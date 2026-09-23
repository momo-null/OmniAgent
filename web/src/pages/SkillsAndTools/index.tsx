import { useCallback, useEffect, useState } from "react";
import {
  Box,
  Tabs,
  Tab,
  Typography,
  List,
  ListItem,
  ListItemText,
  ListItemButton,
  Chip,
  Stack,
  IconButton,
  Alert,
  TextField,
  Button,
  Switch,
  FormControlLabel,
  Divider,
  Tooltip,
  Drawer,
  Dialog,
  DialogTitle,
  DialogContent,
  DialogContentText,
  DialogActions,
  Paper,
  useTheme,
} from "@mui/material";
import BoltIcon from "@mui/icons-material/Bolt";
import DeleteIcon from "@mui/icons-material/Delete";
import AddIcon from "@mui/icons-material/Add";
import SaveIcon from "@mui/icons-material/Save";
import RestartAltIcon from "@mui/icons-material/RestartAlt";
import AutoStoriesIcon from "@mui/icons-material/AutoStories";
import HistoryEduIcon from "@mui/icons-material/HistoryEdu";
import LockIcon from "@mui/icons-material/Lock";
import { skillApi, runtimeApi, settingsApi, memoryApi, signalsApi } from "../../api/client";
import { useTaskStore } from "../../store/taskStore.tsx";
import type {
  SkillInfo,
  ToolInfo,
  ToolsResponse,
  MemoryIndex,
  RolloutInfo,
  RolloutDetail,
  SteadyState,
} from "../../types";

function SkillsTab() {
  const { snapshot, currentTaskId, refreshTasks } = useTaskStore();
  const [error, setError] = useState("");

  const handleRun = async (name: string) => {
    try {
      await skillApi.run({ task_id: currentTaskId || undefined, skill_name: name });
    } catch (e) {
      setError((e as Error).message);
    }
  };
  const handleDelete = async (name: string) => {
    try {
      await skillApi.delete({ task_id: currentTaskId || undefined, skill_name: name });
      refreshTasks();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const skills = snapshot.skills || [];
  return (
    <Box>
      {error && <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError("")}>{error}</Alert>}
      {skills.length === 0 ? (
        <Typography variant="body2" color="text.secondary">暂无技能。运行任务后，成功策略会晋升为技能。</Typography>
      ) : (
        <List dense>
          {skills.map((s: SkillInfo) => (
            <ListItem
              key={s.name}
              secondaryAction={
                <Stack direction="row" spacing={0.5}>
                  <IconButton size="small" onClick={() => handleRun(s.name)} title="调用"><BoltIcon fontSize="small" /></IconButton>
                  <IconButton size="small" onClick={() => handleDelete(s.name)} title="删除"><DeleteIcon fontSize="small" /></IconButton>
                </Stack>
              }
            >
              <ListItemText
                primary={
                  <Stack direction="row" spacing={1} alignItems="center">
                    <span>{s.name}</span>
                    <Chip size="small" label={s.status} />
                    <Typography variant="caption" color="text.secondary">{s.success_count}/{s.total_uses}</Typography>
                  </Stack>
                }
                secondary={s.objective_pattern}
              />
            </ListItem>
          ))}
        </List>
      )}
    </Box>
  );
}

const GROUP_LABEL: Record<string, string> = {
  device: "设备/通用",
  vision: "视觉/SoM",
  python: "Python 执行",
  shell: "Shell 执行（高危）",
  mcp: "外部 MCP",
  generic: "其它",
  filesystem: "文件系统",
  web: "联网",
  local_model: "本地模型",
  skill: "技能（按需加载）",
};

function useTools() {
  const [data, setData] = useState<ToolsResponse | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await runtimeApi.tools();
      setData(res.data as ToolsResponse);
      setError("");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return { data, error, setError, loading, reload: load };
}

function ToolsTab() {
  const { data, error, setError, reload } = useTools();
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState("");
  const [filter, setFilter] = useState("");

  // 单工具禁用名单：以每个工具自带的 disabled 标记为准（接口返回全部工具，含已禁用的）
  const disabled = new Set((data?.tools || []).filter((t) => t.disabled).map((t) => t.name));
  const groupSet = new Set(data?.active_groups || data?.groups || []);

  const toggleGroup = async (g: string) => {
    const next = new Set(groupSet);
    if (next.has(g)) { next.delete(g); } else { next.add(g); }
    setBusy(true); setSaved("");
    try {
      await settingsApi.put({ runtime: { tools: { groups: [...next].sort() } } });
      await reload();
      setSaved(next.has(g) ? `已启用分组 ${g}` : `已关闭分组 ${g}`);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const toggle = async (name: string) => {
    const next = new Set(disabled);
    if (next.has(name)) { next.delete(name); } else { next.add(name); }
    setBusy(true); setSaved("");
    try {
      await runtimeApi.setDisabledTools([...next]);
      await reload();
      setSaved(next.has(name) ? `已禁用 ${name}` : `已启用 ${name}`);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (!data) {
    return <Typography variant="body2" color="text.secondary">加载中…</Typography>;
  }

  const byGroup = new Map<string, ToolInfo[]>();
  for (const t of data.tools) {
    if (filter && !t.name.toLowerCase().includes(filter.toLowerCase())) { continue; }
    const list = byGroup.get(t.group) || [];
    list.push(t);
    byGroup.set(t.group, list);
  }

  return (
    <Box>
      <Alert severity="info" sx={{ mb: 2 }}>
        工具=插件：自研工具与外部 MCP 工具在同一层完全平级，由 LLM 直接调用；内核零持有、零派发。
        右侧开关为「视图级单工具禁用」，写入 <code>runtime.tools.disabled</code>，重启后生效。
      </Alert>
      <Typography variant="subtitle2" gutterBottom>能力分组（runtime.tools.groups）</Typography>
      <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap sx={{ mb: 1 }}>
        {(data.all_groups || [...groupSet]).map((g) => (
          <FormControlLabel key={g} sx={{ mr: 0 }}
            control={<Switch size="small" disabled={busy} checked={groupSet.has(g)}
              onChange={() => void toggleGroup(g)} />}
            label={<Chip size="small" variant="outlined" label={GROUP_LABEL[g] || g}
              color={g === "shell" ? "error" : (groupSet.has(g) ? "primary" : "default")}
              sx={{ opacity: groupSet.has(g) ? 1 : 0.6 }} />} />
        ))}
      </Stack>
      <Typography variant="caption" color="text.secondary">
        分组开关写入 <code>runtime.tools.groups</code>，重启构建注册表后生效；shell 为高危分组，默认关闭。
      </Typography>

      <Stack direction="row" spacing={1} sx={{ mb: 2, mt: 2, maxWidth: 320 }} alignItems="center">
        <TextField label="筛选工具名" size="small" fullWidth value={filter}
          onChange={(e) => setFilter(e.target.value)} />
        <Button size="small" disabled={busy} onClick={() => { void runtimeApi.setDisabledTools([]).then(reload); setSaved("已全部恢复启用"); }}>
          全部启用
        </Button>
      </Stack>
      {error && <Alert severity="error" sx={{ mb: 1 }}>{error}</Alert>}
      {saved && <Alert severity="success" sx={{ mb: 1 }}>{saved}</Alert>}
      {[...byGroup.entries()].map(([group, tools]) => (
        <Box key={group} sx={{ mb: 2 }}>
          <Typography variant="subtitle2" gutterBottom>
            {GROUP_LABEL[group] || group}（{tools.length}）
          </Typography>
          <Stack spacing={0.5}>
            {tools.map((t) => (
              <Stack key={t.name} direction="row" alignItems="center" spacing={1}>
                <Switch size="small" disabled={busy}
                  checked={!disabled.has(t.name)}
                  onChange={() => void toggle(t.name)} />
                <Tooltip title={t.description || t.name}>
                  <Chip size="small" variant="outlined" label={t.name}
                    color={disabled.has(t.name) ? "default" : (t.source === "mcp" ? "secondary" : "primary")}
                    sx={{ opacity: disabled.has(t.name) ? 0.5 : 1 }} />
                </Tooltip>
                {t.source === "mcp" && <Chip size="small" label="MCP" variant="outlined" />}
                {t.group_enabled === false && <Chip size="small" color="warning" variant="outlined" label="分组未启用" />}
              </Stack>
            ))}
          </Stack>
        </Box>
      ))}
    </Box>
  );
}

function McpTab() {
  const { data, error, setError, reload } = useTools();
  const [saved, setSaved] = useState(false);

  // 本地编辑态：name / command / args / url / enabled
  const [servers, setServers] = useState<
    { name: string; enabled: boolean; command: string; args: string; url: string }[]
  >([]);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    if (data && !loaded) {
      setServers(
        data.mcp.servers.map((s) => ({
          name: s.name,
          enabled: s.enabled,
          command: s.command,
          args: (s.args || []).join(" "),
          url: s.url,
        })),
      );
      setLoaded(true);
    }
  }, [data, loaded]);

  const persist = async (next: typeof servers) => {
    try {
      // 只提交 runtime.mcp 一节（后端 deepMerge，不影响其它配置）
      await settingsApi.put({
        runtime: {
          mcp: {
            enabled: true,
            servers: next.map((s) => ({
              name: s.name,
              enabled: s.enabled,
              command: s.command,
              args: s.args.split(/\s+/).filter(Boolean),
              url: s.url,
            })),
          },
        },
      });
      setSaved(true);
      setError("");
      await reload();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const update = (idx: number, patch: Partial<(typeof servers)[number]>) =>
    setServers((prev) => prev.map((s, i) => (i === idx ? { ...s, ...patch } : s)));

  return (
    <Box>
      <Alert severity="info" sx={{ mb: 2 }}>
        MCP 专用于外部工具接入：新增能力 = 加一条 server 配置，零代码改动；接入后其工具与自研工具平级。
        连接在每个任务启动时建立，改动保存后从「下一个任务」起生效。
      </Alert>
      {error && <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError("")}>{error}</Alert>}
      {saved && <Alert severity="success" sx={{ mb: 2 }} onClose={() => setSaved(false)}>已保存</Alert>}

      {data && (
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          runtime.mcp.enabled = {String(data.mcp.enabled)}；已接入：
          {data.mcp.connected.length ? data.mcp.connected.join("、") : "（无）"}
        </Typography>
      )}

      {servers.map((s, i) => (
        <Box key={i} sx={{ mb: 2 }}>
          <Stack direction="row" spacing={1} alignItems="center">
            <TextField
              size="small"
              label="name"
              value={s.name}
              onChange={(e) => update(i, { name: e.target.value })}
            />
            <FormControlLabel
              control={
                <Switch
                  checked={s.enabled}
                  onChange={(e) => update(i, { enabled: e.target.checked })}
                />
              }
              label="启用"
            />
            <IconButton
              size="small"
              onClick={() => {
                const next = servers.filter((_, j) => j !== i);
                setServers(next);
                void persist(next);
              }}
              title="删除"
            >
              <DeleteIcon fontSize="small" />
            </IconButton>
          </Stack>
          <Stack direction="row" spacing={1} sx={{ mt: 1 }}>
            <TextField
              size="small"
              fullWidth
              label="command（stdio，如 npx）"
              value={s.command}
              onChange={(e) => update(i, { command: e.target.value })}
            />
            <TextField
              size="small"
              fullWidth
              label="args（空格分隔）"
              value={s.args}
              onChange={(e) => update(i, { args: e.target.value })}
            />
            <TextField
              size="small"
              fullWidth
              label="url（streamable http，二选一）"
              value={s.url}
              onChange={(e) => update(i, { url: e.target.value })}
            />
          </Stack>
          <Divider sx={{ mt: 2 }} />
        </Box>
      ))}

      <Stack direction="row" spacing={1} sx={{ mt: 1 }}>
        <Button
          size="small"
          startIcon={<AddIcon />}
          onClick={() =>
            setServers((prev) => [
              ...prev,
              { name: "", enabled: true, command: "", args: "", url: "" },
            ])
          }
        >
          新增 server
        </Button>
        <Button size="small" variant="contained" onClick={() => void persist(servers)}>
          保存
        </Button>
      </Stack>
    </Box>
  );
}

// ── K1 全局长期记忆（Memory Tab） ───────────────────────
function MemoryTab() {
  const { memorySignal } = useTaskStore();
  const theme = useTheme();
  const [index, setIndex] = useState<MemoryIndex | null>(null);
  const [rollouts, setRollouts] = useState<RolloutInfo[]>([]);
  const [master, setMaster] = useState("");
  const [editing, setEditing] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");
  const [drawer, setDrawer] = useState<RolloutDetail | null>(null);
  const [resetOpen, setResetOpen] = useState(false);
  const [resetText, setResetText] = useState("");

  const load = useCallback(async () => {
    try {
      const [mi, ro] = await Promise.all([
        memoryApi.index(),
        memoryApi.rollouts({ limit: 100 }),
      ]);
      const m = mi.data as MemoryIndex;
      setIndex(m);
      setMaster(m.master);
      setRollouts((ro.data as { rollouts: RolloutInfo[] }).rollouts || []);
      setError("");
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  // memorySignal 自增（SSE memory_updated）→ 刷新统计；挂载时也拉一次
  useEffect(() => {
    void load();
  }, [load, memorySignal]);

  const handleSave = async () => {
    try {
      await memoryApi.update(master);
      setEditing(false);
      setSaved(true);
      await load();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  // 知识注入开关（memory + skills 一起切，对应「不想学就关开关」）
  const handleToggle = async (next: boolean) => {
    try {
      await settingsApi.put({
        // 只写 memory：skills 开关已随 T2.4（技能目录化）移除，写进去是死配置
        runtime: { knowledge: { memory: { enabled: next } } },
      });
      setIndex((prev) => (prev ? { ...prev, enabled: next } : prev));
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const handleDeleteRollout = async (taskId: string) => {
    try {
      await memoryApi.deleteRollout(taskId);
      setDrawer(null);
      await load();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const handleReset = async () => {
    if (resetText.trim() !== "reset") return;
    try {
      await memoryApi.reset();
      setResetOpen(false);
      setResetText("");
      await load();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <Box>
      {error && (
        <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError("")}>{error}</Alert>
      )}
      {saved && (
        <Alert severity="success" sx={{ mb: 2 }} onClose={() => setSaved(false)}>已保存</Alert>
      )}

      {/* 弱注入声明 */}
      <Alert severity="info" sx={{ mb: 2 }}>
        记忆以弱注入形式进入 system prompt：历史记忆/技能（可能过时，以实际观测为准）。
        默认关闭，需在设置或下方开关显式开启才会注入；本页浏览/编辑始终可用。
      </Alert>

      {/* 统计条 + 开关 */}
      <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap sx={{ mb: 2 }}>
        <Chip
          size="small"
          icon={<AutoStoriesIcon />}
          label={`rollouts: ${index?.rollouts_total ?? 0}`}
          variant="outlined"
        />
        <Chip
          size="small"
          icon={<HistoryEduIcon />}
          label={`已合并: ${index?.merged_total ?? 0}`}
          variant="outlined"
        />
        <Chip
          size="small"
          icon={<LockIcon />}
          label={`注入视图 ${index?.summary_chars ?? 0} 字`}
          variant="outlined"
        />
        <FormControlLabel
          control={
            <Switch
              checked={Boolean(index?.enabled)}
              onChange={(e) => handleToggle(e.target.checked)}
            />
          }
          label="知识注入"
        />
        <Box sx={{ flexGrow: 1 }} />
        <Button
          size="small"
          color="error"
          startIcon={<RestartAltIcon />}
          onClick={() => setResetOpen(true)}
        >
          重置记忆
        </Button>
      </Stack>

      {/* ① MEMORY.md 编辑器（只读 + 编辑切换） */}
      <Typography variant="subtitle2" gutterBottom>全局长期记忆（MEMORY.md）</Typography>
      {editing ? (
        <Box sx={{ mb: 2 }}>
          <TextField
            fullWidth
            multiline
            minRows={10}
            maxRows={24}
            value={master}
            onChange={(e) => setMaster(e.target.value)}
            sx={{ fontFamily: "monospace", fontSize: 13, mb: 1 }}
          />
          <Stack direction="row" spacing={1}>
            <Button size="small" variant="contained" startIcon={<SaveIcon />} onClick={handleSave}>
              保存（重写并再生注入视图）
            </Button>
            <Button size="small" onClick={() => { setMaster(index?.master || ""); setEditing(false); }}>
              取消
            </Button>
          </Stack>
        </Box>
      ) : (
        <Box sx={{ mb: 2 }}>
          <Paper
            elevation={0}
            sx={{
              p: 1.5, borderRadius: 1, bgcolor: "background.paper",
              border: "1px solid", borderColor: "divider",
              maxHeight: 320, overflow: "auto", whiteSpace: "pre-wrap", wordBreak: "break-word",
              fontSize: 13, minHeight: 60,
            }}
          >
            {master ? master : "（MEMORY.md 为空，蒸馏产物合并或你手动编辑后会出现内容）"}
          </Paper>
          <Button size="small" sx={{ mt: 1 }} onClick={() => setEditing(true)}>
            编辑 MEMORY.md
          </Button>
        </Box>
      )}

      <Divider sx={{ my: 2 }} />

      {/* ② rollouts 溯源列表 */}
      <Typography variant="subtitle2" gutterBottom>记忆回放溯源（rollouts）</Typography>
      {rollouts.length === 0 ? (
        <Typography variant="body2" color="text.secondary">
          暂无 rollout。任务完成后 Curator 静默蒸馏（成功任务抽 facts，失败任务记 lessons），每满 5 条未合并项合并进 MEMORY.md。
        </Typography>
      ) : (
        <List dense>
          {rollouts.map((r) => (
            <ListItemButton key={r.task_id} onClick={() => memoryApi.rollout(r.task_id).then((res) => setDrawer(res.data as RolloutDetail)).catch(() => setError("读取 rollout 失败"))}>
              <ListItemText
                primary={
                  <Stack direction="row" spacing={1} alignItems="center">
                    <span style={{ fontFamily: "monospace", fontSize: 13 }}>{r.task_id}</span>
                    {r.success === true && <Chip size="small" label="成功" color="success" />}
                    {r.success === false && <Chip size="small" label="失败" color="warning" />}
                    {r.merged && <Chip size="small" label="已合并" variant="outlined" />}
                  </Stack>
                }
                secondary={`facts:${r.facts_n} · lessons:${r.lessons_n}${r.distilled_at ? " · " + r.distilled_at : ""}`}
              />
            </ListItemButton>
          ))}
        </List>
      )}

      {/* ③ 单条 rollout 全文抽屉 */}
      <Drawer anchor="right" open={Boolean(drawer)} onClose={() => setDrawer(null)} sx={{ zIndex: theme.zIndex.drawer + 1 }}>
        <Box sx={{ width: 460, p: 2, display: "flex", flexDirection: "column", minHeight: 0 }}>
          <Stack direction="row" justifyContent="space-between" alignItems="center" sx={{ mb: 1 }}>
            <Typography variant="subtitle2" noWrap>
              rollout: {drawer?.task_id}
            </Typography>
            <IconButton size="small" onClick={() => setDrawer(null)}><DeleteIcon fontSize="small" /></IconButton>
          </Stack>
          {drawer && (
            <>
              <Typography variant="caption" color="text.secondary" sx={{ mb: 1 }}>
                trajectory: {drawer.trajectory || "（无）"} · facts:{drawer.facts_n} · lessons:{drawer.lessons_n}
              </Typography>
              <ScrollAreaBox sx={{ flexGrow: 1, minHeight: 0 }}>
                <Box component="pre" sx={{ whiteSpace: "pre-wrap", wordBreak: "break-word", fontSize: 12, m: 0 }}>
                  {drawer.content}
                </Box>
              </ScrollAreaBox>
              <Button
                size="small" color="error" sx={{ mt: 1 }} startIcon={<DeleteIcon />}
                onClick={() => drawer && handleDeleteRollout(drawer.task_id)}
              >
                删除此 rollout（不回滚已合并内容）
              </Button>
            </>
          )}
        </Box>
      </Drawer>

      {/* ④ 重置确认对话框（输入 reset） */}
      <Dialog open={resetOpen} onClose={() => setResetOpen(false)} maxWidth="xs" fullWidth>
        <DialogTitle>重置全部记忆</DialogTitle>
        <DialogContent>
          <DialogContentText>
            将清空 memory/ 全部产物（MEMORY.md / 注入视图 / 全部 rollouts / 合并清单）。
            此操作不可恢复，且不触及任何任务私有数据。输入 <b>reset</b> 确认。
          </DialogContentText>
          <TextField
            autoFocus fullWidth margin="dense" label="输入 reset"
            value={resetText} onChange={(e) => setResetText(e.target.value)}
          />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => { setResetOpen(false); setResetText(""); }}>取消</Button>
          <Button color="error" disabled={resetText.trim() !== "reset"} onClick={handleReset}>
            确认重置
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}

// 轻量可滚动容器（复用 ScrollArea 组件避免重复实现）
function ScrollAreaBox({ children, sx }: { children: React.ReactNode; sx?: Record<string, unknown> }) {
  return (
    <Box sx={{ overflow: "auto", ...(sx || {}) }}>
      {children}
    </Box>
  );
}

export default function SkillsAndTools() {
  const [tab, setTab] = useState(0);
  return (
    <Box>
      <Typography variant="h6" gutterBottom>技能与工具</Typography>
      <Tabs value={tab} onChange={(_, v) => setTab(v)} sx={{ mb: 2 }}>
        <Tab label="Skills" />
        <Tab label="Tools" />
        <Tab label="MCP" />
        <Tab label="记忆" />
        <Tab label="稳态" />
      </Tabs>
      {tab === 0 && <SkillsTab />}
      {tab === 1 && <ToolsTab />}
      {tab === 2 && <McpTab />}
      {tab === 3 && <MemoryTab />}
      {tab === 4 && <SteadyTab />}
    </Box>
  );

// ── K5 稳态 Tab（域健康度面板） ───────────────────────────────
function fmtPct(v?: number | null) {
  return v == null ? "-" : `${(v * 100).toFixed(1)}%`;
}

// 轻量 SVG 衰减曲线（无第三方图表依赖）
function DecayCurve({ data, threshold }: { data: number[]; threshold: number }) {
  const W = 460, H = 160, pad = 24;
  const n = data.length;
  const x = (i: number) => pad + (n <= 1 ? 0 : (i / (n - 1)) * (W - 2 * pad));
  const y = (v: number) => H - pad - Math.max(0, Math.min(1, v)) * (H - 2 * pad);
  const pts = data.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const ty = y(threshold);
  return (
    <Box sx={{ border: "1px solid", borderColor: "divider", borderRadius: 1, p: 1 }}>
      <svg width="100%" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="xMidYMid meet">
        <line x1={pad} y1={ty} x2={W - pad} y2={ty} stroke="#e57373" strokeDasharray="4 4" />
        <text x={W - pad} y={ty - 4} fontSize="10" fill="#e57373" textAnchor="end">
          阈值 {fmtPct(threshold)}
        </text>
        <polyline points={pts} fill="none" stroke="#42a5f5" strokeWidth="2" />
        <line x1={pad} y1={H - pad} x2={W - pad} y2={H - pad} stroke="#999" />
        <line x1={pad} y1={pad} x2={pad} y2={H - pad} stroke="#999" />
      </svg>
    </Box>
  );
}

function SteadyTab() {
  const { memorySignal } = useTaskStore();
  const [steady, setSteady] = useState<SteadyState | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      const r = await signalsApi.steady();
      setSteady(r.data);
      setError("");
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  // 任务结束（memorySignal 自增）或挂载时刷新稳态面板
  useEffect(() => { void load(); }, [load, memorySignal]);

  const s = steady?.signals;
  const tl = s?.intervention_timeline || [];
  return (
    <Box>
      {error && <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError("")}>{error}</Alert>}
      <Alert severity="info" sx={{ mb: 2 }}>
        域健康度面板（K5，v1 单域假设）：四信号量化「人机协同蒸馏至稳态」。
        介入频率衰减曲线是核心演示面——越低表示你越不需要纠偏。
      </Alert>

      <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap sx={{ mb: 2 }}>
        <Chip label={`域: ${steady?.domain ?? "-"}`} variant="outlined" />
        <Chip
          label={steady?.converged ? "已收敛 ✓" : "收敛中…"}
          color={steady?.converged ? "success" : "default"}
        />
        <Chip label={`蒸馏去重命中率: ${fmtPct(s?.distill_dedup_hit_rate)}`} variant="outlined" />
        <Chip label={`skill 晋升率: ${fmtPct(s?.skill_promotion_rate.rate)}`} variant="outlined" />
        <Chip label={`步数方差: ${s?.step_variance.variance ?? "-"}`} variant="outlined" />
        <Chip label={`介入频率: ${fmtPct(s?.human_intervention_rate.rate)}`} variant="outlined" />
      </Stack>

      <Typography variant="subtitle2" gutterBottom>
        人工介入频率衰减曲线（滚动窗口 refuted 占比，目标 ≤5%）
      </Typography>
      {tl.length === 0 ? (
        <Typography variant="body2" color="text.secondary">
          暂无交互信号。任务跑出若干轮含纠偏/认可的对话后，曲线会自动绘制。
        </Typography>
      ) : (
        <DecayCurve data={tl} threshold={0.05} />
      )}

      <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1 }}>
        判据初值：去重命中率 ≥80% 且介入频率 ≤5% → 收敛（Curator 自动降频，跳过蒸馏）。
        阈值标注「初值，实测校准」；校准误差需 C₃ 人工抽检填写（见 GET /signals/summary）。
      </Typography>
    </Box>
  );
}
}
