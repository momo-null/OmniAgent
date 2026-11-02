import { useCallback, useEffect, useState } from "react";
import {
  Box,
  Button,
  Chip,
  IconButton,
  Paper,
  Stack,
  Switch,
  TextField,
  Typography,
} from "@mui/material";
import {
  BrainIcon,
  GlobeIcon,
  PlusIcon,
  ServerIcon,
  TrashIcon,
  WrenchIcon,
  ZapIcon,
} from "../../components/icons";
import NavOverlay, { NavCategory } from "../../components/NavOverlay";
import { Group, Hint, SettingRow, StatusLine, SwitchRow } from "../../components/SettingsKit";
import { skillApi, runtimeApi, settingsApi } from "../../api/client";
import { useTaskStore } from "../../store/taskStore.tsx";
import type { SkillInfo, ToolInfo, ToolsResponse } from "../../types";

// 技能与工具：与设置同一套桌面遮罩外壳（分类导航 + 分组行），原来的三个 Tabs 已删除。

/** 技能：一条一行，左名称+适用场景，右计数/来源/操作 */
function SkillsSection() {
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
    <Group title="技能" desc="任务成功后由内核提炼的可复用策略；点闪电立即调用一次。">
      {skills.length === 0 ? (
        <Typography variant="body2" color="text.secondary">
          暂无技能。运行任务后，成功策略会晋升为技能。
        </Typography>
      ) : (
        skills.map((s: SkillInfo) => (
          <SettingRow key={s.name} title={s.name} desc={s.objective_pattern}>
            <Stack direction="row" spacing={1} alignItems="center">
              <Typography variant="caption" color="text.secondary" sx={{ flexShrink: 0 }}>
                成 {s.success_count}/{s.total_uses}
              </Typography>
              <Chip size="small" label={s.status} sx={{ height: 18, fontSize: 11 }} />
              {s.scope && (
                <Chip size="small" variant="outlined" label={s.scope === "global" ? "全局" : "项目"}
                  sx={{ height: 18, fontSize: 11 }} />
              )}
              <IconButton size="small" onClick={() => void handleRun(s.name)} title="调用">
                <ZapIcon fontSize="small" />
              </IconButton>
              {s.scope === "project" && (
                <IconButton size="small" onClick={() => void handlePromote(s.name)} title="设为全局">
                  <GlobeIcon fontSize="small" />
                </IconButton>
              )}
              <IconButton size="small" onClick={() => void handleDelete(s.name)} title="删除"
                sx={{ color: "text.disabled", "&:hover": { color: "error.main" } }}>
                <TrashIcon fontSize="small" />
              </IconButton>
            </Stack>
          </SettingRow>
        ))
      )}
      <StatusLine error={error} />
    </Group>
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

const SOURCE_LABEL: Record<string, string> = { core: "内核", plugin: "插件", mcp: "MCP" };

/** 只读工具行：左名称（等宽），右来源与提供者 */
function ToolRow({ t }: { t: ToolInfo }) {
  // unit = 提供者标识：环境 kind / 插件名；core 源与来源重复不显示
  const provider = t.source === "plugin" ? t.unit : (t.source === "mcp" ? (t.server || "") : "");
  return (
    <Box sx={{ display: "flex", alignItems: "center", gap: 1.5, py: 0.85,
               borderTop: "1px solid", borderColor: "divider" }}>
      <Typography variant="body2" noWrap title={t.description || t.name}
        sx={{ flex: 1, minWidth: 0, fontFamily: "monospace", fontSize: 12.5 }}>
        {t.name}
      </Typography>
      <Chip size="small" variant="outlined" label={SOURCE_LABEL[t.source] ?? t.source}
        sx={{ height: 18, fontSize: 11, flexShrink: 0 }} />
      {provider && (
        <Typography variant="caption" color="text.disabled" noWrap sx={{ flexShrink: 0, maxWidth: 150 }}>
          {provider}
        </Typography>
      )}
    </Box>
  );
}

/** 工具：插件一行一开关，内核/MCP 工具只读列出 */
function ToolsSection() {
  const { data, error, setError, reload } = useTools();
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState("");

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
      <Group title="插件" desc="每个插件一枚开关，写 ~/.omniagent/plugins/<name>.yaml；操作电脑 / Android 也是普通插件，默认关闭。开关重启后生效。">
        {data.plugins.length === 0 ? (
          <Typography variant="body2" color="text.secondary">（无插件）</Typography>
        ) : (
          data.plugins.map((p) => (
            <SwitchRow key={p.name}
              title={p.title} desc={p.description}
              checked={p.enabled} disabled={busy}
              onChange={(v) => void togglePlugin(p.name, v)}
            />
          ))
        )}
        <StatusLine saved={saved} error={error} />
      </Group>

      <Group title={`当前可用工具（${data.tools.length}，只读）`}>
        {data.tools.map((t) => <ToolRow key={`${t.source}:${t.name}`} t={t} />)}
        <Hint>工具由内核、启用的插件与已接入的 MCP 合并而来；这里不给开关，停用请回到上一栏关插件或删 MCP 配置。</Hint>
      </Group>
    </Box>
  );
}

/** MCP 接入：本地编辑态，改动要点「保存」才写回（多字段编辑器保留显式提交） */
function McpSection() {
  const { data, error, setError, reload } = useTools();
  const [saved, setSaved] = useState(false);

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
      <Group title="MCP 接入" desc="新增外部能力 = 加一条 server 配置，零代码改动；接入后其工具与自研工具平级。连接在每个任务启动时建立，改动保存后从下一个任务起生效。">
        <Hint>
          {data
            ? `runtime.mcp.enabled = ${String(data.mcp.enabled)}；已接入：${data.mcp.connected.length ? data.mcp.connected.join("、") : "（无）"}`
            : "读取中…"}
        </Hint>

        {servers.map((s, i) => (
          <Paper key={i} variant="outlined" sx={{ mt: 2, p: 2 }}>
            <Stack direction="row" spacing={1} alignItems="center">
              <TextField
                size="small" label="名称" value={s.name}
                onChange={(e) => update(i, { name: e.target.value })}
                sx={{ width: 190, "& .MuiOutlinedInput-notchedOutline": { borderColor: "divider" } }}
              />
              <SwitchRowInline label="启用" checked={s.enabled} onChange={(v) => update(i, { enabled: v })} />
              <IconButton
                size="small"
                sx={{ ml: "auto", color: "text.disabled", "&:hover": { color: "error.main" } }}
                onClick={() => {
                  const next = servers.filter((_, j) => j !== i);
                  setServers(next);
                  void persist(next);
                }}
                title="删除该 server"
              >
                <TrashIcon fontSize="small" />
              </IconButton>
            </Stack>
            <Stack spacing={1.5} sx={{ mt: 1.5 }}>
              <TextField
                size="small" fullWidth label="command（stdio，如 npx）" value={s.command}
                onChange={(e) => update(i, { command: e.target.value })}
                sx={{ "& .MuiOutlinedInput-notchedOutline": { borderColor: "divider" },
                      "& input": { fontFamily: "monospace", fontSize: 12.5 } }}
              />
              <TextField
                size="small" fullWidth label="args（空格分隔）" value={s.args}
                onChange={(e) => update(i, { args: e.target.value })}
                sx={{ "& .MuiOutlinedInput-notchedOutline": { borderColor: "divider" },
                      "& input": { fontFamily: "monospace", fontSize: 12.5 } }}
              />
              <TextField
                size="small" fullWidth label="url（streamable http，与 command 二选一）" value={s.url}
                onChange={(e) => update(i, { url: e.target.value })}
                sx={{ "& .MuiOutlinedInput-notchedOutline": { borderColor: "divider" },
                      "& input": { fontFamily: "monospace", fontSize: 12.5 } }}
              />
            </Stack>
          </Paper>
        ))}

        <Stack direction="row" spacing={1} sx={{ mt: 2 }}>
          <Button
            size="small" variant="outlined" startIcon={<PlusIcon />}
            onClick={() =>
              setServers((prev) => [
                ...prev,
                { name: "", enabled: true, command: "", args: "", url: "" },
              ])
            }
          >
            新增 server
          </Button>
          <Button size="small" variant="contained" onClick={() => void persist(servers)}>保存</Button>
        </Stack>
        <StatusLine saved={saved ? "已保存，下一个任务起生效" : ""} error={error} />
      </Group>
    </Box>
  );
}

// 卡片内的一枚「标签 + 开关」，不另起一行设置项
function SwitchRowInline({ label, checked, onChange }:
  { label: string; checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <Box sx={{ display: "flex", alignItems: "center", gap: 0.5 }}>
      <Switch size="small" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      <Typography variant="body2" color="text.secondary">{label}</Typography>
    </Box>
  );
}

const CATEGORIES: NavCategory[] = [
  {
    id: "skills", label: "技能", icon: <BrainIcon />, keywords:
      ["playbook", "策略", "提炼", "全局", "项目", "调用", "成功次数"],
    Section: SkillsSection,
  },
  {
    id: "tools", label: "工具", icon: <WrenchIcon />, keywords:
      ["插件", "开关", "内核工具", "操作电脑", "android", "plugins", "只读", "来源"],
    Section: ToolsSection,
  },
  {
    id: "mcp", label: "MCP", icon: <ServerIcon />, keywords:
      ["外部工具", "server", "stdio", "npx", "streamable http", "url", "command", "args", "接入"],
    Section: McpSection,
  },
];

export default function SkillsAndTools({ onClose }: { onClose: () => void }) {
  return <NavOverlay title="技能与工具" categories={CATEGORIES} onClose={onClose} />;
}
