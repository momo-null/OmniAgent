import { useEffect, useRef, useState } from "react";
import {
  Alert,
  Box,
  Button,
  Chip,
  FormControl,
  InputLabel,
  LinearProgress,
  List,
  ListItemButton,
  ListItemText,
  ListSubheader,
  MenuItem,
  Paper,
  Select,
  Slider,
  Stack,
  TextField,
  Typography,
} from "@mui/material";
import { LabelWithTip, StatusLine } from "../../components/SettingsKit";
import { modelApi, llmApi, systemApi, settingsApi } from "../../api/client";
import type { ModelInfo, GpuInfo, LaunchParams, ModelValidationResult } from "../../types";
import ModelProviders from "./ModelProviders";
import VectorEngineCard from "./VectorEngineCard";
import VoiceEngineCard from "./VoiceEngineCard";

// ── 透传参数（extra_args）与投影路径的「UI ↔ 侧注」映射 ──────────────────────
// extra_args：多行文本，**每行一个 argv token** ⇒ 与后端 argv 数组一一对应，
// 值里含空格（如带空格的路径）也能表达；留空 = 不写该字段（等价于无额外参数）。
const parseArgs = (text: string): string[] =>
  text.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);

// 侧注的 mmproj_path 按约定写「相对 GGUF 同目录」以保持模型目录可移植；
// 扫描接口返回的是解析后的绝对路径 ⇒ 保存时若在同一目录内就转回相对。
const toRelMmproj = (abs: string, ggufPath: string): string => {
  const dir = ggufPath.replace(/[\\/][^\\/]*$/, "");
  if (!dir || !abs.startsWith(dir)) return abs;
  return abs.slice(dir.length).replace(/^[\\/]+/, "");
};

const buildBody = (p?: LaunchParams): Record<string, unknown> => {
  const b: Record<string, unknown> = {};
  if (!p) return b;
  if (p.gguf_path) b.gguf_path = p.gguf_path;
  if (p.threads != null) b.threads = p.threads;
  if (p.ctx_size != null) b.ctx_size = p.ctx_size;
  if (p.gpu_layers != null) b.gpu_layers = p.gpu_layers;
  if (p.port != null) b.port = p.port;
  if (p.reasoning_budget != null) b.reasoning_budget = p.reasoning_budget;
  if (p.extra_args && p.extra_args.length > 0) b.extra_args = p.extra_args;
  if (p.profile) b.profile = p.profile;
  return b;
};

const initDraft = (m: ModelInfo): LaunchParams => ({
  threads: m.threads, ctx_size: m.ctx_size, gpu_layers: m.gpu_layers,
  port: m.port, reasoning_budget: m.reasoning_budget, profile: null,
  // 侧注字段回填：支持编辑往返（清空 mmproj_path 即回退"自动探测同目录"）
  extra_args: m.extra_args ?? [], mmproj_path: m.mmproj_path ?? null,
});

const buildSaveBody = (m: ModelInfo, d: LaunchParams): Record<string, unknown> => {
  const b: Record<string, unknown> = { gguf_path: m.gguf_path };
  if (d.ctx_size != null) b.ctx_size = d.ctx_size;
  if (d.gpu_layers != null) b.gpu_layers = d.gpu_layers;
  if (d.threads != null) b.threads = d.threads;
  b.port = d.port ?? null;
  if (d.reasoning_budget != null) b.reasoning_budget = d.reasoning_budget;
  // 透传参数：留空 → 传 null，让后端从侧注删除该字段（回退"无额外参数"）
  b.extra_args = (d.extra_args && d.extra_args.length > 0) ? d.extra_args : null;
  // 投影：清空 → 删除字段（回退自动探测）；填了 → 同目录内转相对路径保持可移植
  b.mmproj_path = d.mmproj_path ? toRelMmproj(d.mmproj_path, m.gguf_path) : null;
  return b;
};

// 启动参数编辑器：这些是 llama.cpp 的进程级参数，属于「高级」，
// 保持紧凑表单（一行一字段），不做成设置行的左标题右控件——字段太密会撑得很长。
function ParamEditor({ draft, setDraft }: { draft: LaunchParams; setDraft: (p: LaunchParams) => void }) {
  return (
    <Stack spacing={2} sx={{ mt: 1, mb: 1 }}>
      <TextField label="线程 threads" type="number" size="small" value={draft.threads ?? ""}
        onChange={(e) => setDraft({ ...draft, threads: e.target.value ? Number(e.target.value) : null })} />
      <TextField label="上下文 ctx_size" type="number" size="small" value={draft.ctx_size ?? ""}
        onChange={(e) => setDraft({ ...draft, ctx_size: e.target.value ? Number(e.target.value) : null })} />
      <Box>
        <Typography variant="body2" gutterBottom>GPU 层数 gpu_layers: {draft.gpu_layers ?? 99}</Typography>
        <Slider value={draft.gpu_layers ?? 99} min={0} max={99} step={1} size="small"
          onChange={(_, v) => setDraft({ ...draft, gpu_layers: v as number })} />
      </Box>
      <TextField label="端口 port（留空=自动分配）" type="number" size="small" value={draft.port ?? ""}
        onChange={(e) => setDraft({ ...draft, port: e.target.value ? Number(e.target.value) : null })} />
      <TextField label="推理预算 reasoning_budget" type="number" size="small" value={draft.reasoning_budget ?? ""}
        onChange={(e) => setDraft({ ...draft, reasoning_budget: e.target.value ? Number(e.target.value) : null })} />
      <TextField
        label={<LabelWithTip text="投影文件 mmproj_path（留空 = 自动探测同目录）"
          desc="显式指定多模态投影文件（llama.cpp --mmproj）。留空时启动前自动探测 GGUF 同目录下文件名含 mmproj 的 .gguf；填同一目录内的文件会按相对路径写回侧注，保持模型目录可移植。需要不加载投影时：把这里写成一个不存在的路径即可（自动探测会被跳过，启动日志有提示）。" />}
        size="small" fullWidth value={draft.mmproj_path ?? ""}
        onChange={(e) => setDraft({ ...draft, mmproj_path: e.target.value || null })} />
      <TextField
        label={<LabelWithTip text="额外启动参数（每行一个，透传 llama.cpp）"
          desc="模型启动参数很精细，不可能全部做成控件。除内核掌管的 -m / --host / --port / --mmproj 外，其余参数都可透传（如 -fa、on、--no-mmproj-offload、-ctk、q8_0）。每行一个参数（argv token），追加在启动命令末尾，并写入模型侧注 .meta.json（随模型走，不进项目配置）。留空 = 无额外参数。" />}
        size="small" fullWidth multiline minRows={3} maxRows={10}
        placeholder={"-fa\non"}
        value={(draft.extra_args ?? []).join("\n")}
        onChange={(e) => setDraft({ ...draft, extra_args: parseArgs(e.target.value) })}
        sx={{ "& textarea": { fontFamily: "monospace", fontSize: 12 } }} />
      <FormControl size="small">
        <InputLabel id="profile-label">预设 profile</InputLabel>
        <Select labelId="profile-label" label="预设 profile" value={draft.profile ?? ""}
          onChange={(e) => setDraft({ ...draft, profile: e.target.value || null })}>
          <MenuItem value="">无</MenuItem>
          <MenuItem value="default">default</MenuItem>
          <MenuItem value="openclaw">openclaw（省显存）</MenuItem>
        </Select>
      </FormControl>
    </Stack>
  );
}

function ModelDetail({ model, draft, setDraft, busy, saving, validateBusy, onStart, onStop, onValidate, onSave, validateResult }:
  { model: ModelInfo; draft: LaunchParams; setDraft: (p: LaunchParams) => void; busy: string; saving: string; validateBusy: string; onStart: (m: ModelInfo) => void; onStop: (n: string) => void; onValidate: (n: string) => void; onSave: (m: ModelInfo) => void; validateResult: ModelValidationResult | null; }) {
  const name = model.name;
  const running = model.status === "running";
  const isBusy = busy === name; const isSaving = saving === name;
  const showValidate = validateResult && validateResult.name === name;
  return (
    <Paper variant="outlined" sx={{ p: 2 }}>
      <Stack direction="row" justifyContent="space-between" alignItems="center">
        <Box>
          <Typography variant="subtitle1">{name} <Chip size="small" label={running ? "运行中" : (model.has_meta ? "已配置" : "默认")} color={running ? "success" : "default"} /></Typography>
          <Typography variant="body2" color="text.secondary">{model.description || model.gguf_path} ｜ {model.quant} ｜ {model.size_mb} MB{model.tags.length > 0 ? ` ｜ ${model.tags.join("/")}` : ""}{model.port ? ` ｜ 端口 ${model.port}` : " ｜ 端口 自动"}</Typography>
        </Box>
      </Stack>
      <Stack direction="row" spacing={1} sx={{ mt: 1 }}>
        <Button size="small" variant="contained" disabled={running || isBusy} onClick={() => onStart(model)}>{isBusy ? "..." : "启动"}</Button>
        <Button size="small" variant="outlined" color="secondary" disabled={!running || isBusy} onClick={() => onStop(name)}>停止</Button>
        <Button size="small" variant="outlined" disabled={isBusy || validateBusy === name || !running} onClick={() => onValidate(name)}>{validateBusy === name ? "校验中..." : "校验"}</Button>
        <Button size="small" variant="outlined" color="primary" disabled={isSaving} onClick={() => onSave(model)}>{isSaving ? "保存中..." : (model.has_meta ? "更新配置" : "保存配置")}</Button>
      </Stack>
      <Typography variant="subtitle2" sx={{ mt: 2 }}>启动参数</Typography>
      <Box sx={{ maxWidth: 480 }}><ParamEditor draft={draft} setDraft={setDraft} /></Box>
      {showValidate && validateResult && (
        <Box sx={{ mt: 1, p: 1, bgcolor: "action.hover", borderRadius: 1 }}>
          <Typography variant="subtitle2">校验结果 {validateResult.ok ? "✅" : "❌"}</Typography>
          {validateResult.base_url && (
            <Typography variant="body2" sx={{ fontFamily: "monospace" }}>base_url: {validateResult.base_url}</Typography>
          )}
          {validateResult.model && (
            <Typography variant="body2" sx={{ fontFamily: "monospace" }}>model: {validateResult.model}</Typography>
          )}
          {validateResult.error && <Alert severity="error" sx={{ my: 1 }}>{validateResult.error}</Alert>}
          <Typography variant="body2" sx={{ whiteSpace: "pre-wrap", fontFamily: "monospace" }}>{validateResult.reply || "（无回复）"}</Typography>
        </Box>
      )}
    </Paper>
  );
}

export default function ModelSection() {
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [drafts, setDrafts] = useState<Record<string, LaunchParams>>({});
  // 扫描目录来自配置 local_model.models_dir（后端下发生效值）；失焦即持久化
  const [modelsDir, setModelsDir] = useState<string>("");
  const modelsDirRef = useRef("");
  const savedDirRef = useRef("");
  const [gpu, setGpu] = useState<GpuInfo | null>(null);
  const [error, setError] = useState<string>("");
  const [busy, setBusy] = useState<string>("");
  const [saving, setSaving] = useState<string>("");
  const [saveMsg, setSaveMsg] = useState<string>("");
  const [validateResult, setValidateResult] = useState<ModelValidationResult | null>(null);
  const [validateBusy, setValidateBusy] = useState<string>("");
  const [selName, setSelName] = useState<string>("");

  const refresh = async () => {
    try {
      const [m, g] = await Promise.all([modelApi.list(modelsDirRef.current || undefined), systemApi.gpu()]);
      setModels(m.data as ModelInfo[]);
      setGpu((g.data as GpuInfo) ?? null);
    } catch (e) { setError((e as Error).message); }
  };

  useEffect(() => {
    setDrafts((prev) => { const next = { ...prev }; for (const m of models) if (!next[m.name]) next[m.name] = initDraft(m); return next; });
  }, [models]);

  // A：去掉 5s 自动轮询，界面保持安静；状态更新只在显式刷新 / 启停 / 校验后发生
  useEffect(() => {
    // 先取配置里的扫描目录（后端下发生效值），再按它刷新模型列表
    settingsApi.get().then((r) => {
      const md = ((r.data as Record<string, unknown>)?.local_model as Record<string, unknown> | undefined)?.models_dir;
      const dir = typeof md === "string" ? md : "";
      modelsDirRef.current = dir;
      savedDirRef.current = dir;
      setModelsDir(dir);
    }).catch(() => {}).finally(() => { refresh(); });
  }, []);

  // 目录失焦：与已保存值不同则写回 local_model.models_dir（配置化，前端不再写死路径）
  const handleDirBlur = () => {
    const v = modelsDir.trim();
    if (v === savedDirRef.current) { refresh(); return; }
    settingsApi.put({ local_model: { models_dir: v } })
      .then(() => { savedDirRef.current = v; setSaveMsg("扫描目录已保存到配置"); })
      .catch((e) => setError((e as Error).message))
      .finally(() => { refresh(); });
  };

  const handleStart = async (m: ModelInfo) => {
    setBusy(m.name); setError("");
    try { const d = drafts[m.name] ?? initDraft(m); await modelApi.start(m.name, buildBody({ ...d, gguf_path: m.gguf_path })); await refresh(); }
    catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  };
  const handleStop = async (name: string) => {
    setBusy(name); try { await modelApi.stop(name); await refresh(); } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  };
  const handleValidate = async (name: string) => {
    setValidateBusy(name); setError("");
    try { const r = await modelApi.validate(name, { prompt: "请用一句话介绍自己。" }); setValidateResult(r.data as ModelValidationResult); }
    catch (e) { setError((e as Error).message); } finally { setValidateBusy(""); }
  };
  const handleSave = async (m: ModelInfo) => {
    setSaving(m.name); setError(""); setSaveMsg("");
    try { const d = drafts[m.name] ?? initDraft(m); await modelApi.save(m.name, buildSaveBody(m, d)); setSaveMsg(`已保存 ${m.name} 到 .meta.json`); await refresh(); }
    catch (e) { setError((e as Error).message); } finally { setSaving(""); }
  };
  const handleApiTest = async () => {
    try { const r = await llmApi.models(); setSaveMsg(JSON.stringify(r.data)); } catch (e) { setError((e as Error).message); }
  };
  const handleKillOrphans = async () => {
    if (!window.confirm("强制释放全部 llama-server 进程？含后端崩溃残留的孤儿，且会停掉当前已启动模型。")) return;
    setBusy("__all__"); setError("");
    try { await modelApi.killOrphans(); setSaveMsg("已强制释放全部 llama-server 进程"); await refresh(); }
    catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  };

  const total = gpu?.memory_total_mb ?? 0;
  const used = gpu?.memory_used_mb ?? 0;
  const pct = total > 0 ? (used / total) * 100 : 0;

  const selected = models.find((m) => m.name === selName) ?? models[0] ?? null;

  return (
    <Box>
      {error && <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError("")}>{error}</Alert>}

      {/* 在线模型目录（models.json）：与本机服务分开，放最上面 */}
      <ModelProviders />

      {/* 语音引擎与本地模型同页：同样是「本机跑、可切在线」的一类引擎 */}
      <VoiceEngineCard />

      {/* 向量服务：与语音同形态，端点配置 + 服务启动两层开关 */}
      <VectorEngineCard />

      {/* 本地模型（llama.cpp）：紧挨模型列表，配置与列表同组阅读 */}
      <Paper variant="outlined" sx={{ mb: 2, p: 2 }}>
        <Stack direction="row" justifyContent="space-between" alignItems="center">
          <Typography variant="subtitle2">本地模型（llama.cpp）</Typography>
          <Button size="small" onClick={refresh}>刷新</Button>
        </Stack>
        {gpu && !gpu.error ? (
          <>
            <Typography variant="body2" sx={{ mt: 1 }}>{gpu.name}</Typography>
            <LinearProgress variant="determinate" value={pct} sx={{ my: 1 }} />
            <Typography variant="caption" color="text.secondary">显存 {used.toFixed(0)} / {total.toFixed(0)} MB（{pct.toFixed(1)}%）</Typography>
          </>
        ) : <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>{gpu?.error ?? "GPU 信息不可用"}</Typography>}
        <Stack direction="row" spacing={1} alignItems="center" sx={{ mt: 1.5 }}>
          <TextField label="模型目录（扫描根目录，失焦自动保存到配置）" size="small" fullWidth value={modelsDir}
            onChange={(e) => setModelsDir(e.target.value)} onBlur={handleDirBlur} />
          <Button size="small" variant="outlined" onClick={refresh}>扫描</Button>
        </Stack>

        <Stack direction="row" spacing={1} sx={{ mt: 1.5 }}>
          <Button size="small" variant="outlined" onClick={() => modelApi.stopAll()}>停止全部</Button>
          <Button size="small" variant="outlined" color="error" disabled={busy === "__all__"} onClick={handleKillOrphans}>{busy === "__all__" ? "释放中..." : "强制释放全部"}</Button>
          <Button size="small" variant="outlined" onClick={handleApiTest}>API 连通测试</Button>
        </Stack>
        <StatusLine saved={saveMsg} />
      </Paper>

      <Box sx={{ display: "flex", gap: 2, alignItems: "flex-start" }}>
        <Paper variant="outlined" sx={{ width: 300, flexShrink: 0, maxHeight: "70vh", overflow: "auto" }}>
          <List dense>
            <ListSubheader>模型列表（{models.length}）</ListSubheader>
            {models.map((m) => (
              <ListItemButton key={m.name} selected={selected?.name === m.name} onClick={() => setSelName(m.name)}>
                <ListItemText primary={m.name}
                  secondary={`${m.status === "running" ? "运行中" : "未运行"}${m.port ? " · 端口 " + m.port : ""}`} />
              </ListItemButton>
            ))}
            {models.length === 0 && <ListItemText primary="（无模型，先扫描目录）" sx={{ px: 2 }} />}
          </List>
        </Paper>
        <Box sx={{ flex: 1, minWidth: 0 }}>
          {selected ? (
            <ModelDetail model={selected} draft={drafts[selected.name] ?? initDraft(selected)} setDraft={(p) => setDrafts((d) => ({ ...d, [selected.name]: p }))}
              busy={busy} saving={saving} validateBusy={validateBusy}
              onStart={handleStart} onStop={handleStop} onValidate={handleValidate} onSave={handleSave}
              validateResult={validateResult} />
          ) : <Alert severity="info">左侧选择模型以编辑启动参数</Alert>}
        </Box>
      </Box>
    </Box>
  );
}
