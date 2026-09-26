import { useCallback, useEffect, useRef, useState } from "react";
import {
  Box,
  Tabs,
  Tab,
  Typography,
  List,
  ListItem,
  ListItemText,
  Chip,
  Stack,
  IconButton,
  Alert,
  TextField,
  Button,
  Switch,
  Radio,
  FormControlLabel,
  Divider,
  Tooltip,
} from "@mui/material";
import BoltIcon from "@mui/icons-material/Bolt";
import DeleteIcon from "@mui/icons-material/Delete";
import AddIcon from "@mui/icons-material/Add";
import { skillApi, runtimeApi, settingsApi, signalsApi } from "../../api/client";
import { useTaskStore } from "../../store/taskStore.tsx";
import type {
  SkillInfo,
  ToolsResponse,
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

  const setEnv = async (kind: string) => {
    setBusy(true); setSaved("");
    try {
      await runtimeApi.setEnvironment(kind);
      await reload();
      setSaved(`已切换环境：${kind}（重启后生效）`);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const togglePlugin = async (name: string, enabled: boolean) => {
    setBusy(true); setSaved("");
    try {
      await runtimeApi.setPluginEnabled(name, enabled);
      await reload();
      setSaved(enabled ? `已启用插件 ${name}` : `已停用插件 ${name}`);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (!data) {
    return <Typography variant="body2" color="text.secondary">加载中…</Typography>;
  }

  return (
    <Box>
      <Alert severity="info" sx={{ mb: 2 }}>
        三类：<b>环境</b>（单选，自带整套工具，写 <code>runtime.backend</code>）、
        <b>插件</b>（各自一个开关，写 <code>~/.omniagent/plugins/&lt;name&gt;.yaml</code>）、
        <b>工具</b>（只读）。切换 / 开关重启后生效。
      </Alert>

      <Typography variant="subtitle2" gutterBottom>环境（单选）</Typography>
      <Stack direction="row" spacing={2} flexWrap="wrap" useFlexGap sx={{ mb: 2 }}>
        {data.environments.map((e) => (
          <FormControlLabel key={e.kind} sx={{ mr: 0 }}
            control={<Radio size="small" disabled={busy} checked={e.active}
              onChange={() => { if (!e.active) void setEnv(e.kind); }} />}
            label={<Chip size="small" variant="outlined" label={e.title}
              color={e.active ? "primary" : "default"}
              sx={{ opacity: e.active ? 1 : 0.6 }} />} />
        ))}
      </Stack>

      <Typography variant="subtitle2" gutterBottom>插件（各自一个开关）</Typography>
      <Stack direction="row" spacing={2} flexWrap="wrap" useFlexGap sx={{ mb: 2 }}>
        {data.plugins.map((p) => (
          <FormControlLabel key={p.name} sx={{ mr: 0 }}
            control={<Switch size="small" disabled={busy} checked={p.enabled}
              onChange={(_e, v) => void togglePlugin(p.name, v)} />}
            label={<Tooltip title={p.description || p.name}>
              <Chip size="small" variant="outlined" label={p.title}
                color={p.enabled ? "primary" : "default"}
                sx={{ opacity: p.enabled ? 1 : 0.6 }} />
            </Tooltip>} />
        ))}
        {data.plugins.length === 0 && (
          <Typography variant="caption" color="text.secondary">（无插件）</Typography>
        )}
      </Stack>

      {error && <Alert severity="error" sx={{ mb: 1 }}>{error}</Alert>}
      {saved && <Alert severity="success" sx={{ mb: 1 }}>{saved}</Alert>}

      <Typography variant="subtitle2" gutterBottom>当前可用工具（只读）</Typography>
      <Stack spacing={0.5}>
        {data.tools.map((t) => (
          <Stack key={t.name} direction="row" alignItems="center" spacing={1}>
            <Tooltip title={t.description || t.name}>
              <Chip size="small" variant="outlined" label={t.name}
                color={t.source === "mcp" ? "secondary" : (t.source === "plugin" ? "success" : "primary")} />
            </Tooltip>
            <Chip size="small" variant="outlined" label={t.source} />
            {t.source === "env" && <Chip size="small" variant="outlined" label={t.unit} />}
            {t.source === "mcp" && t.server && <Chip size="small" label={t.server} variant="outlined" />}
          </Stack>
        ))}
      </Stack>
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


export default function SkillsAndTools() {
  const [tab, setTab] = useState(0);
  return (
    <Box>
      <Typography variant="h6" gutterBottom>技能与工具</Typography>
      <Tabs value={tab} onChange={(_, v) => setTab(v)} sx={{ mb: 2 }}>
        <Tab label="Skills" />
        <Tab label="Tools" />
        <Tab label="MCP" />
        <Tab label="稳态" />
      </Tabs>
      {tab === 0 && <SkillsTab />}
      {tab === 1 && <ToolsTab />}
      {tab === 2 && <McpTab />}
      {tab === 3 && <SteadyTab />}
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
  // 稳态是低频聚合指标：内容去重（无实质变化不重渲染）+ 防抖（合并连发），消除刷新闪烁
  const lastKeyRef = useRef<string>("");
  const debounceRef = useRef<number | null>(null);

  const load = useCallback(async () => {
    try {
      const d = (await signalsApi.steady()).data as SteadyState;
      const key = JSON.stringify([
        d?.domain,
        d?.converged,
        d?.signals?.distill_dedup_hit_rate,
        d?.signals?.skill_promotion_rate?.rate,
        d?.signals?.step_variance?.variance,
        d?.signals?.human_intervention_rate?.rate,
        d?.signals?.intervention_timeline,
      ]);
      // 内容未变 → 不 setState，避免反复重渲染造成闪烁
      if (lastKeyRef.current === key) return;
      lastKeyRef.current = key;
      setSteady(d);
      setError("");
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  // 挂载即拉一次
  useEffect(() => { void load(); }, [load]);
  // 任务结束（memorySignal 自增）→ 防抖后再拉，合并连发/重连，避免高频刷新闪烁
  useEffect(() => {
    if (debounceRef.current) window.clearTimeout(debounceRef.current);
    debounceRef.current = window.setTimeout(() => { void load(); }, 1200);
    return () => { if (debounceRef.current) window.clearTimeout(debounceRef.current); };
  }, [memorySignal, load]);

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
