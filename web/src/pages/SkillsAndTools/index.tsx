import { useCallback, useEffect, useState } from "react";
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
  Paper,
  Tooltip,
} from "@mui/material";
import BoltIcon from "@mui/icons-material/Bolt";
import DeleteIcon from "@mui/icons-material/Delete";
import AddIcon from "@mui/icons-material/Add";
import PublicIcon from "@mui/icons-material/Public";
import { skillApi, runtimeApi, settingsApi } from "../../api/client";
import { useTaskStore } from "../../store/taskStore.tsx";
import type {
  SkillInfo,
  ToolsResponse,
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
  // A7 手动提升：唯一到 global 的路径（用户显式「设为全局」）
  const handlePromote = async (name: string) => {
    try {
      await skillApi.promote({ task_id: currentTaskId || undefined, skill_name: name });
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
                  {s.scope === "project" && (
                    <IconButton size="small" onClick={() => handlePromote(s.name)} title="设为全局"><PublicIcon fontSize="small" /></IconButton>
                  )}
                  <IconButton size="small" onClick={() => handleDelete(s.name)} title="删除"><DeleteIcon fontSize="small" /></IconButton>
                </Stack>
              }
            >
              <ListItemText
                primary={
                  <Stack direction="row" spacing={1} alignItems="center">
                    <Typography variant="body2" component="span">{s.name}</Typography>
                    <Chip size="small" label={s.status} />
                    {s.scope && <Chip size="small" label={s.scope === "global" ? "全局" : "项目"} variant="outlined" />}
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

      <Paper variant="outlined" sx={{ p: 2, mb: 2 }}>
        <Typography variant="subtitle2" gutterBottom>环境（单选）</Typography>
        <Stack direction="row" spacing={2} flexWrap="wrap" useFlexGap>
          {data.environments.map((e) => (
            <FormControlLabel key={e.kind} sx={{ mr: 0 }}
              control={<Radio size="small" disabled={busy} checked={e.active}
                onChange={() => { if (!e.active) void setEnv(e.kind); }} />}
              label={<Chip size="small" variant={e.active ? "filled" : "outlined"} label={e.title}
                color={e.active ? "primary" : "default"}
                sx={{ opacity: e.active ? 1 : 0.6 }} />} />
          ))}
        </Stack>
      </Paper>

      <Paper variant="outlined" sx={{ p: 2, mb: 2 }}>
        <Typography variant="subtitle2" gutterBottom>插件（各自一个开关）</Typography>
        <Stack direction="row" spacing={2} flexWrap="wrap" useFlexGap>
          {data.plugins.map((p) => (
            <FormControlLabel key={p.name} sx={{ mr: 0 }}
              control={<Switch size="small" disabled={busy} checked={p.enabled}
                onChange={(_e, v) => void togglePlugin(p.name, v)} />}
              label={<Tooltip title={p.description || p.name}>
                <Chip size="small" variant={p.enabled ? "filled" : "outlined"} label={p.title}
                  color={p.enabled ? "primary" : "default"}
                  sx={{ opacity: p.enabled ? 1 : 0.6 }} />
              </Tooltip>} />
          ))}
          {data.plugins.length === 0 && (
            <Typography variant="caption" color="text.secondary">（无插件）</Typography>
          )}
        </Stack>
      </Paper>

      {error && <Alert severity="error" sx={{ mb: 1 }} onClose={() => setError("")}>{error}</Alert>}
      {saved && <Alert severity="success" sx={{ mb: 1 }}>{saved}</Alert>}

      <Paper variant="outlined" sx={{ p: 2 }}>
        <Typography variant="subtitle2" gutterBottom>当前可用工具（只读）</Typography>
        <Stack spacing={0.5}>
          {data.tools.map((t) => (
            <Stack key={t.name} direction="row" alignItems="center" spacing={1}>
              <Tooltip title={t.description || t.name}>
                <Chip size="small" variant="outlined" label={t.name}
                  color={t.source === "mcp" ? "secondary" : (t.source === "plugin" ? "success" : "primary")} />
              </Tooltip>
              <Chip size="small" variant="outlined" label={t.source} />
              {/* unit = 提供者标识：环境 kind / 插件名；core 源与 source 重复不显示 */}
              {(t.source === "env" || t.source === "plugin") && <Chip size="small" variant="outlined" label={t.unit} />}
              {t.source === "mcp" && t.server && <Chip size="small" label={t.server} variant="outlined" />}
            </Stack>
          ))}
        </Stack>
      </Paper>
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
        <Paper key={i} variant="outlined" sx={{ mb: 2, p: 2 }}>
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
        </Paper>
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
    <Box sx={{ p: 2, pt: 1.5, maxWidth: 960 }}>
      <Typography variant="subtitle1" gutterBottom sx={{ fontWeight: 600 }}>技能与工具</Typography>
      <Tabs value={tab} onChange={(_, v) => setTab(v)} sx={{ mb: 2 }}>
        <Tab label="Skills" />
        <Tab label="Tools" />
        <Tab label="MCP" />
      </Tabs>
      {tab === 0 && <SkillsTab />}
      {tab === 1 && <ToolsTab />}
      {tab === 2 && <McpTab />}
    </Box>
  );
}
