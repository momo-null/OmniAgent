import { useEffect, useState } from "react";
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  Divider,
  IconButton,
  List,
  ListItemButton,
  ListItemText,
  ListSubheader,
  Menu,
  MenuItem,
  Paper,
  Stack,
  Switch,
  TextField,
  Tooltip,
  Typography,
} from "@mui/material";
import AddIcon from "@mui/icons-material/Add";
import DeleteOutlineIcon from "@mui/icons-material/DeleteOutline";
import InfoOutlinedIcon from "@mui/icons-material/InfoOutlined";
import { modelCatalogApi } from "../../api/client";
import type { ModelProviderDraft, ModelsIndex } from "../../types";

/**
 * 在线模型目录（提供方 + 模型清单），与本地模型同页展示。
 *
 * 目录独立存放于 ~/.omniagent/models.json：厂商/模型信息不进通用 config.yaml，
 * 是端点的**唯一真源**（旧通道 brain / runtime.executor / llm.local_as_tool 已下线，
 * 可经「导入旧配置」一次性迁入目录后删除）。
 * 前端不预设任何厂商——只提供「添加」，由用户填 base_url / 模型 id。
 */

// 槽位 = 模型的用途；目录里为槽位选了模型 = 启用该用途
const SLOTS: { key: string; label: string; hint: string }[] = [
  { key: "main", label: "主模型", hint: "agent 的大脑，规划/反思/执行一条龙" },
  { key: "worker", label: "子 agent", hint: "主 agent 派发子任务时用的模型；未选 = 主模型兼任" },
];

export default function ModelProviders() {
  const [catalog, setCatalog] = useState<ModelsIndex | null>(null);
  const [drafts, setDrafts] = useState<Record<string, ModelProviderDraft>>({});
  const [sel, setSel] = useState<string>("");
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const [newId, setNewId] = useState("");
  const [newModel, setNewModel] = useState("");
  const [slotAnchor, setSlotAnchor] = useState<{ el: HTMLElement; selection: string } | null>(null);

  const load = () => {
    modelCatalogApi.list().then((r) => {
      const d = r.data as ModelsIndex;
      setCatalog(d);
      setDrafts(
        Object.fromEntries(
          (d?.providers || []).map((p) => [
            p.id,
            {
              label: p.label,
              base_url: p.base_url,
              api_key: "", // 明文不回传；留空 = 保持不变
              models: p.models.map((m) => ({ ...m })),
            },
          ]),
        ),
      );
      setSel((prev) => prev || (d?.providers?.[0]?.id ?? ""));
    }).catch((e) => setErr((e as Error).message));
  };

  useEffect(() => { load(); }, []);

  const ids = Object.keys(drafts);
  const cur = sel ? drafts[sel] : null;
  const patch = (id: string, next: Partial<ModelProviderDraft>) =>
    setDrafts((d) => ({ ...d, [id]: { ...d[id], ...next } }));

  const addProvider = () => {
    const id = newId.trim();
    if (!id || drafts[id]) return;
    setDrafts((d) => ({ ...d, [id]: { label: id, base_url: "", api_key: "", models: [] } }));
    setSel(id);
    setNewId("");
  };
  const removeProvider = (id: string) => {
    setDrafts((d) => { const n = { ...d }; delete n[id]; return n; });
    if (sel === id) setSel("");
  };
  const addModel = (id: string) => {
    const mid = newModel.trim();
    if (!mid) return;
    const p = drafts[id];
    if (p.models.some((m) => m.id === mid)) return;
    patch(id, { models: [...p.models, { id: mid, label: "", vision: false }] });
    setNewModel("");
  };

  const save = async () => {
    setErr(""); setMsg("");
    try {
      await modelCatalogApi.save(drafts);
      setMsg("已保存");
      load();
    } catch (e) { setErr((e as Error).message); }
  };
  const setDefault = async (slot: string, selection: string) => {
    setErr(""); setMsg("");
    try {
      await modelCatalogApi.setDefault(slot, selection);
      setMsg(`已设为 ${SLOTS.find((s) => s.key === slot)?.label ?? slot}：${selection}`);
      load();
    } catch (e) { setErr((e as Error).message); }
  };
  const slotState = (key: string) => {
    const c = catalog?.current?.[key];
    if (c?.source === "catalog") return c.model;
    return "未选";
  };

  return (
    <Card variant="outlined" sx={{ mb: 2 }}>
      <CardContent>
        <Stack direction="row" justifyContent="space-between" alignItems="center">
          <Stack direction="row" spacing={0.5} alignItems="center">
            <Typography variant="subtitle2">在线模型</Typography>
            <Tooltip title="模型端点的唯一配置入口（独立于通用配置存放）；Chat 输入框的选择器按这里的提供方分组。不预设厂商，自行添加。">
              <InfoOutlinedIcon sx={{ fontSize: 15, color: "text.disabled", cursor: "help" }} />
            </Tooltip>
          </Stack>
        </Stack>
        <Stack spacing={0.5} sx={{ mb: 1.5, mt: 1 }}>
          {SLOTS.map((s) => (
            <Stack key={s.key} direction="row" spacing={0.5} alignItems="center">
              <Typography variant="caption" color="text.secondary">{s.label}：</Typography>
              <Typography variant="caption">{slotState(s.key)}</Typography>
              <Tooltip title={s.hint}>
                <InfoOutlinedIcon sx={{ fontSize: 14, color: "text.disabled", cursor: "help" }} />
              </Tooltip>
            </Stack>
          ))}
        </Stack>

        {err && <Alert severity="error" sx={{ mb: 1 }} onClose={() => setErr("")}>{err}</Alert>}
        {msg && <Alert severity="success" sx={{ mb: 1 }} onClose={() => setMsg("")}>{msg}</Alert>}

        <Stack direction="row" spacing={1} sx={{ mb: 1.5 }}>
          <TextField
            label="新提供方标识（英文 key，如 online / local）" size="small" value={newId}
            onChange={(e) => setNewId(e.target.value)} sx={{ flex: 1 }}
          />
          <Button variant="outlined" startIcon={<AddIcon />} onClick={addProvider} disabled={!newId.trim() || !!drafts[newId.trim()]}>
            添加
          </Button>
        </Stack>

        <Box sx={{ display: "flex", gap: 2, alignItems: "flex-start" }}>
          <Paper variant="outlined" sx={{ width: 240, flexShrink: 0, maxHeight: 320, overflow: "auto" }}>
            <List dense>
              <ListSubheader>提供方（{ids.length}）</ListSubheader>
              {ids.map((id) => (
                <ListItemButton key={id} selected={sel === id} onClick={() => setSel(id)}>
                  <ListItemText
                    primary={drafts[id].label || id}
                    secondary={`${drafts[id].models.length} 个模型${catalog?.providers.find((p) => p.id === id)?.api_key_set ? " · 已配 key" : ""}`}
                  />
                </ListItemButton>
              ))}
              {ids.length === 0 && <ListItemText primary="（尚无提供方，先添加一个）" sx={{ px: 2 }} />}
            </List>
          </Paper>

          <Box sx={{ flex: 1, minWidth: 0 }}>
            {!cur ? (
              <Alert severity="info">左侧选择提供方以编辑</Alert>
            ) : (
              <Stack spacing={1.5}>
                <Stack direction="row" spacing={1}>
                  <TextField label="显示名" size="small" fullWidth value={cur.label}
                    onChange={(e) => patch(sel, { label: e.target.value })} />
                  <TextField label="标识（不可改）" size="small" value={sel} disabled sx={{ width: 160 }} />
                </Stack>
                <TextField label="Base URL（OpenAI 兼容端点）" size="small" fullWidth
                  placeholder="https://api.example.com/v1" value={cur.base_url}
                  onChange={(e) => patch(sel, { base_url: e.target.value })} />
                <TextField
                  label="API Key（留空 = 保持不变）" size="small" fullWidth type="password"
                  placeholder={catalog?.providers.find((p) => p.id === sel)?.api_key_set ? "已配置，留空保持不变" : ""}
                  value={cur.api_key} onChange={(e) => patch(sel, { api_key: e.target.value })}
                />
                <TextField label="环境变量名（可选，无明文 key 时使用）" size="small" fullWidth
                  placeholder="OMNI_BRAIN_API_KEY" value={cur.api_key_env ?? ""}
                  onChange={(e) => patch(sel, { api_key_env: e.target.value })} />

                <Divider />
                <Typography variant="body2">模型清单</Typography>
                <Stack spacing={1}>
                  {cur.models.map((m, i) => (
                    <Stack key={`${m.id}-${i}`} direction="row" spacing={1} alignItems="center">
                      <TextField label="模型 id" size="small" value={m.id} sx={{ flex: 1 }}
                        onChange={(e) => patch(sel, { models: cur.models.map((x, j) => (j === i ? { ...x, id: e.target.value } : x)) })} />
                      <TextField label="显示名" size="small" value={m.label} sx={{ flex: 1 }}
                        onChange={(e) => patch(sel, { models: cur.models.map((x, j) => (j === i ? { ...x, label: e.target.value } : x)) })} />
                      <Tooltip title="视觉能力">
                        <Switch size="small" checked={m.vision}
                          onChange={(e) => patch(sel, { models: cur.models.map((x, j) => (j === i ? { ...x, vision: e.target.checked } : x)) })} />
                      </Tooltip>
                      <Chip size="small" label="设为用途 ▾" clickable
                        onClick={(e) => setSlotAnchor({ el: e.currentTarget, selection: `${sel}/${m.id}` })} />
                      <IconButton size="small"
                        onClick={() => patch(sel, { models: cur.models.filter((_, j) => j !== i) })}>
                        <DeleteOutlineIcon fontSize="small" />
                      </IconButton>
                    </Stack>
                  ))}
                  {cur.models.length === 0 && (
                    <Typography variant="caption" color="text.secondary">（无模型，添加一个后才能被选择）</Typography>
                  )}
                  <Stack direction="row" spacing={1}>
                    <TextField label="新增模型 id" size="small" value={newModel}
                      onChange={(e) => setNewModel(e.target.value)} sx={{ flex: 1 }} />
                    <Button size="small" variant="outlined" startIcon={<AddIcon />}
                      disabled={!newModel.trim()} onClick={() => addModel(sel)}>添加模型</Button>
                  </Stack>
                </Stack>

                <Stack direction="row" spacing={1} justifyContent="flex-end" sx={{ mt: 1 }}>
                  <Button size="small" color="error" onClick={() => removeProvider(sel)}>删除提供方</Button>
                  <Button size="small" variant="contained" onClick={save}>保存</Button>
                </Stack>
              </Stack>
            )}
          </Box>
        </Box>

        <Menu
          anchorEl={slotAnchor?.el ?? null}
          open={Boolean(slotAnchor)}
          onClose={() => setSlotAnchor(null)}
          anchorOrigin={{ vertical: "bottom", horizontal: "left" }}
        >
          {SLOTS.map((s) => (
            <MenuItem key={s.key} dense onClick={() => {
              if (slotAnchor) setDefault(s.key, slotAnchor.selection);
              setSlotAnchor(null);
            }}>
              设为{s.label}
            </MenuItem>
          ))}
        </Menu>
      </CardContent>
    </Card>
  );
}
