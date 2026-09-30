import { useEffect, useRef, useState } from "react";
import {
  Box,
  Tabs,
  Tab,
  Typography,
  Card,
  CardContent,
  Stack,
  Alert,
  Chip,
  LinearProgress,
  TextField,
  Button,
  FormControl,
  FormControlLabel,
  InputLabel,
  Select,
  MenuItem,
  Slider,
  List,
  ListItemButton,
  ListItemText,
  ListSubheader,
  Switch,
  Tooltip,
  Paper,
  Divider,
} from "@mui/material";
import InfoOutlinedIcon from "@mui/icons-material/InfoOutlined";

// 上下文治理缺省值镜像（与后端 config.CONTEXT_DEFAULTS 保持一致）：仅作首屏占位，
// 接口返回后一律以后端「生效值」为准——缺省由代码决定，不在前端生成推荐值 UI。
const CTX_RECOMMENDED = {
  maxInputTokens: 300000, // 输入预算（Token）；当下主流大模型多为 1M 上下文，取 300k 起步
  retainRatio: 0.5,
  pruneThreshold: 8192,
  pruneHead: 4096,
  pruneTail: 1024,
  repeatGuard: 3,
};
import { modelApi, llmApi, systemApi, settingsApi } from "../../api/client";
import type {
  ModelInfo,
  GpuInfo,
  LaunchParams,
  ModelValidationResult,
} from "../../types";
import { useTaskStore } from "../../store/taskStore.tsx";
import CharacterTab from "./CharacterTab";
import ProfileTab from "./ProfileTab";
import MemoryTab from "./MemoryTab";
import SecurityTab from "./SecurityTab";
import ModelProviders from "./ModelProviders";

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
    <Card><CardContent>
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
    </CardContent></Card>
  );
}

function ModelTab() {
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
      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
      {saveMsg && <Alert severity="success" sx={{ mb: 2 }} onClose={() => setSaveMsg("")}>{saveMsg}</Alert>}
      {/* 在线模型目录（models.json）与本地模型同页：上=在线提供方，下=本地 llama.cpp 模型 */}
      <ModelProviders />
      <Typography variant="subtitle2" sx={{ mb: 1 }}>本地模型（llama.cpp）</Typography>
      <Card sx={{ mb: 2 }}><CardContent>
        <Stack direction="row" justifyContent="space-between" alignItems="center">
          <Typography variant="subtitle2">GPU 监控</Typography>
          <Button size="small" onClick={refresh}>刷新</Button>
        </Stack>
        {gpu && !gpu.error ? (
          <><Typography variant="body2">{gpu.name}</Typography>
            <LinearProgress variant="determinate" value={pct} sx={{ my: 1 }} />
            <Typography variant="body2">显存 {used.toFixed(0)} / {total.toFixed(0)} MB（{pct.toFixed(1)}%）</Typography></>
        ) : <Typography variant="body2" color="text.secondary">{gpu?.error ?? "GPU 信息不可用"}</Typography>}
      </CardContent></Card>
      <Card sx={{ mb: 2 }}><CardContent>
        <Stack direction="row" spacing={1} alignItems="center">
          <TextField label="模型目录（扫描根目录，失焦自动保存到配置）" size="small" fullWidth value={modelsDir}
            onChange={(e) => setModelsDir(e.target.value)} onBlur={handleDirBlur} />
          <Button variant="outlined" onClick={refresh}>扫描</Button>
        </Stack>
      </CardContent></Card>
      <Stack direction="row" spacing={1} sx={{ mb: 2 }}>
        <Button variant="outlined" color="secondary" onClick={() => modelApi.stopAll()}>停止全部</Button>
        <Button variant="outlined" color="error" disabled={busy === "__all__"} onClick={handleKillOrphans}>{busy === "__all__" ? "释放中..." : "强制释放全部"}</Button>
        <Button variant="outlined" onClick={handleApiTest}>API 连通测试</Button>
      </Stack>
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

function LabelWithTip({ text, desc }: { text: string; desc?: string }) {
  return (
    <Box component="span" sx={{ display: "inline-flex", alignItems: "center", gap: 0.5 }}>
      <span>{text}</span>
      {desc && (
        <Tooltip title={desc} arrow placement="top">
          <InfoOutlinedIcon sx={{ fontSize: 15, color: "text.disabled", cursor: "help" }} />
        </Tooltip>
      )}
    </Box>
  );
}

function GeneralTab() {
  const { setMaxSteps } = useTaskStore();
  const [draft, setDraft] = useState<number>(40);
  const [memInject, setMemInject] = useState<boolean>(false);
  const [saved, setSaved] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    settingsApi.get().then((r) => {
      const d = r.data as any;
      const v = d?.runtime?.default_max_steps;
      if (typeof v === "number" && v > 0) { setDraft(v); setMaxSteps(v); }
      const mem = (d?.runtime?.knowledge?.memory || {}).enabled;
      if (typeof mem === "boolean") setMemInject(mem);
    }).catch(() => {});
  }, [setMaxSteps]);
  const handleSave = async () => {
    setError(""); setSaved("");
    try {
      const v = draft > 0 ? Math.min(draft, 2000) : 40;
      await settingsApi.put({
        runtime: {
          default_max_steps: v,
          knowledge: { memory: { enabled: memInject } },
        },
      });
      setMaxSteps(v);
      setSaved("已保存默认步数上限与记忆注入开关到 ~/.omniagent/config.yaml");
    } catch (e) { setError((e as Error).message); }
  };
  return (
    <Box>
      <Typography variant="subtitle2" gutterBottom>通用</Typography>
      <Stack spacing={2} sx={{ maxWidth: 360 }}>
        <TextField label={<LabelWithTip text="默认步数上限" desc="单次任务的工具/决策总步数预算，耗尽即停止（budget_exhausted）。前端每次会话默认采用此值；后端 chat 接口未显式传 max_steps 时也会回退到此值。范围 1–2000。" />} type="number" value={draft}
          inputProps={{ min: 1, max: 2000 }}
          onChange={(e) => setDraft(Math.min(Number(e.target.value) || 40, 2000))} />
        <FormControlLabel control={<Switch checked={memInject} onChange={(e) => setMemInject(e.target.checked)} />}
          label={<LabelWithTip text="注入全局记忆摘要 (runtime.knowledge.memory.enabled)" desc="开启后，每轮任务会在系统提示尾部追加全局长期记忆摘要（~/.omniagent/memory 的 memory_summary.md）；关闭则不注入（诚实基线，默认关闭）。" />} />
        <Button variant="contained" onClick={handleSave} sx={{ alignSelf: "flex-start" }}>保存</Button>
        {saved && <Alert severity="success">{saved}</Alert>}
        {error && <Alert severity="error">{error}</Alert>}
        <Alert severity="info">数据根目录：<code>~/.omniagent/</code>（全局层）。任务资产按 task 平铺存放，不依赖工作目录。</Alert>
      </Stack>
    </Box>
  );
}

function OrchestrationTab() {
  const [maxRounds, setMaxRounds] = useState<number>(3);
  const [maxParallel, setMaxParallel] = useState<number>(4);
  const [budgetRatio, setBudgetRatio] = useState<number>(0.75);
  const [chunkTurns, setChunkTurns] = useState<number>(50);
  const [ltEnabled, setLtEnabled] = useState<boolean>(true);
  const [ltMaxTurns, setLtMaxTurns] = useState<number>(16);
  const [ltCompress, setLtCompress] = useState<boolean>(true);
  // 上下文治理（高级）：工具输出修剪 / 粘性压缩 / 重复失败防护
  // 初值取推荐值（后端未配置时不显示裸 0，避免误以为功能关闭）
  const [pruneThreshold, setPruneThreshold] = useState<number>(CTX_RECOMMENDED.pruneThreshold);
  const [pruneHead, setPruneHead] = useState<number>(CTX_RECOMMENDED.pruneHead);
  const [pruneTail, setPruneTail] = useState<number>(CTX_RECOMMENDED.pruneTail);
  const [maxInputTokens, setMaxInputTokens] = useState<number>(CTX_RECOMMENDED.maxInputTokens);
  const [retainRatio, setRetainRatio] = useState<number>(CTX_RECOMMENDED.retainRatio);
  const [repeatGuard, setRepeatGuard] = useState<number>(CTX_RECOMMENDED.repeatGuard);
  const [saved, setSaved] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    settingsApi.get().then((r) => {
      const d = r.data as any;
      const rt = d?.runtime || {};
      const disp = rt.dispatch || {};
      const lt = rt.long_task || {};
      if (typeof disp.max_rounds === "number") setMaxRounds(disp.max_rounds);
      if (typeof disp.max_parallel === "number") setMaxParallel(disp.max_parallel);
      if (typeof lt.budget_hint_ratio === "number") setBudgetRatio(lt.budget_hint_ratio);
      if (typeof rt.chunk_turns === "number") setChunkTurns(rt.chunk_turns);
      const blt = d?.brain?.long_task || {};
      if (typeof blt.enabled === "boolean") setLtEnabled(blt.enabled);
      if (typeof blt.max_turns === "number") setLtMaxTurns(blt.max_turns);
      if (typeof blt.compress === "boolean") setLtCompress(blt.compress);
      // 上下文治理：后端返回的是「代码缺省 + 项目配置 + 用户配置」的生效值，直接展示
      const pr = blt.prune || {};
      if (typeof pr.threshold_chars === "number") setPruneThreshold(pr.threshold_chars);
      if (typeof pr.head_chars === "number") setPruneHead(pr.head_chars);
      if (typeof pr.tail_chars === "number") setPruneTail(pr.tail_chars);
      if (typeof blt.retain_ratio === "number") setRetainRatio(blt.retain_ratio);
      if (typeof lt.repeat_guard === "number") setRepeatGuard(lt.repeat_guard);
      // 旧通道：输入预算挂在 brain.maxInputTokens（新 schema 在 provider 上配）
      const mit = d?.brain?.maxInputTokens ?? d?.brain?.max_input_tokens;
      if (typeof mit === "number") setMaxInputTokens(mit);
    }).catch(() => {});
  }, []);
  const handleSave = async () => {
    setError(""); setSaved("");
    try {
      await settingsApi.put({
        runtime: {
          dispatch: { max_rounds: maxRounds || 3, max_parallel: maxParallel || 4 },
          long_task: { budget_hint_ratio: budgetRatio, repeat_guard: repeatGuard || 0 },
          chunk_turns: chunkTurns || 50,
        },
        brain: {
          maxInputTokens: maxInputTokens || 0,
          long_task: {
            enabled: ltEnabled,
            max_turns: ltMaxTurns || 16,
            compress: ltCompress,
            prune: {
              threshold_chars: pruneThreshold || 0,
              head_chars: pruneHead || 0,
              tail_chars: pruneTail || 0,
            },
            retain_ratio: retainRatio,
          },
        },
      });
      setSaved("已保存编排/长任务配置到 ~/.omniagent/config.yaml");
    } catch (e) { setError((e as Error).message); }
  };
  return (
    <Box>
      <Typography variant="subtitle2" gutterBottom>编排 / 长任务</Typography>
      <Stack spacing={2} sx={{ maxWidth: 480 }}>
        <TextField label={<LabelWithTip text="派发最大轮次 (dispatch.max_rounds)" desc="「派发 → 回收」轮次上限，防止主 agent 无限派发子任务。" />} type="number" value={maxRounds}
          onChange={(e) => setMaxRounds(Number(e.target.value) || 3)} />
        <TextField label={<LabelWithTip text="派发最大并发 (dispatch.max_parallel)" desc="单轮最多并发多少个子 agent。" />} type="number" value={maxParallel}
          onChange={(e) => setMaxParallel(Number(e.target.value) || 4)} />
        <TextField label={<LabelWithTip text="预算提示触发比例 (long_task.budget_hint_ratio)" desc="任务预算用到该比例时，给 agent 一条「已用 X/Y 步」陈述性提示（仅陈述事实，不催促）。0 = 关闭。" />} type="number" value={budgetRatio}
          onChange={(e) => setBudgetRatio(Number(e.target.value) || 0.75)} />
        <TextField label={<LabelWithTip text="分块步数 (runtime.chunk_turns)" desc="SDK 单次 Runner.run 内部步数分块大小；块与块之间做终止/升级检查点。一般保持 50。" />} type="number" value={chunkTurns}
          onChange={(e) => setChunkTurns(Number(e.target.value) || 50)} />

        <Typography variant="subtitle2" sx={{ mt: 1 }}>单大脑长任务压缩（不启本地模型时生效）</Typography>
        <FormControlLabel control={<Switch checked={ltEnabled} onChange={(e) => setLtEnabled(e.target.checked)} />}
          label={<LabelWithTip text="启用长任务压缩 (brain.long_task.enabled)" desc="未在「模型」页给子 agent（worker）选模型时，主模型单大脑长跑会按间隔压缩历史；关闭则不做压缩。" />} />
        <TextField label={<LabelWithTip text="压缩间隔 (brain.long_task.max_turns)" desc="单大脑路径下，每跑 N 轮把历史压缩成一段摘要，防止上下文溢出。用完不会停任务，只控制压缩频率。" />} type="number" value={ltMaxTurns}
          onChange={(e) => setLtMaxTurns(Number(e.target.value) || 16)} />
        <FormControlLabel control={<Switch checked={ltCompress} onChange={(e) => setLtCompress(e.target.checked)} />}
          label={<LabelWithTip text="调用主模型生成摘要 (brain.long_task.compress)" desc="开启：压缩时调用主模型生成中文摘要（更省上下文）。关闭：退化为截断兜底，不消耗额外调用。" />} />

        <Divider />
        <Typography variant="subtitle2" sx={{ mt: 1 }}>上下文治理（高级）</Typography>
        <TextField label={<LabelWithTip text="模型上下文上限 (brain.maxInputTokens)" desc="该模型的输入 Token 上限。填 0 = 关闭 Model 适配层粘性压缩；>0 时按此上限动态计算压缩阈值（前缀复用，省 token）。" />} type="number" value={maxInputTokens}
          helperText="缺省 300000（代码内置；1M 上下文模型可填 1000000）；填 0 = 显式关闭粘性压缩"
          onChange={(e) => setMaxInputTokens(Number(e.target.value) || 0)} />
        <TextField label={<LabelWithTip text="粘性压缩尾部保留比例 (brain.long_task.retain_ratio)" desc="超阈值时，尾部会话保留「上限 × 该比例」的 Token，其余压成摘要。留空/0 = 取默认 0.5。" />} type="number" value={retainRatio}
          inputProps={{ min: 0, max: 0.9, step: 0.05 }}
          onChange={(e) => setRetainRatio(Number(e.target.value) || 0.5)} />
        <TextField label={<LabelWithTip text="工具输出修剪阈值 (prune.threshold_chars)" desc="压缩前先把超长的工具输出做首尾截断，避免单个大 output 撑爆上下文。0 = 关闭（零干预）；建议 8192 左右。" />} type="number" value={pruneThreshold}
          onChange={(e) => setPruneThreshold(Number(e.target.value) || 0)} />
        <TextField label={<LabelWithTip text="修剪保留头部 (prune.head_chars)" desc="超阈值时保留的开头字符数，建议 4096。" />} type="number" value={pruneHead}
          onChange={(e) => setPruneHead(Number(e.target.value) || 0)} />
        <TextField label={<LabelWithTip text="修剪保留尾部 (prune.tail_chars)" desc="超阈值时保留的结尾字符数（多为结果/报错关键信息），建议 1024。" />} type="number" value={pruneTail}
          onChange={(e) => setPruneTail(Number(e.target.value) || 0)} />
        <TextField label={<LabelWithTip text="重复失败提醒次数 (runtime.long_task.repeat_guard)" desc="同一工具连续失败达该次数后，下一次请求自动注入一条「换个方案」的提醒（只注入一次）。0 = 关闭，默认 3。" />} type="number" value={repeatGuard}
          onChange={(e) => setRepeatGuard(Number(e.target.value) || 0)} />

        <Button variant="contained" onClick={handleSave} sx={{ alignSelf: "flex-start" }}>保存</Button>
        {saved && <Alert severity="success">{saved}</Alert>}
        {error && <Alert severity="error">{error}</Alert>}
      </Stack>
    </Box>
  );
}


function AboutTab() {
  return (
    <Box>
      <Typography variant="subtitle2" gutterBottom>关于</Typography>
      <Typography variant="body2" color="text.secondary">
        OmniAgent · 通用 agent 编排内核（Plan C：project + task 两实体）。<br />
        主 agent 规划并执行；可在模型页为多个槽位配置模型，主 agent 自主决定派发；知识库自学（world-model / skill）。<br />
        内核零场景硬编码；能力来自工具与运行时自学。
      </Typography>

      <Typography variant="subtitle2" sx={{ mt: 3 }} gutterBottom>执行路径与配置生效范围</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
        引擎有两条执行路径，同一份配置里只有部分键在对应路径生效——别混：
      </Typography>

      <Card variant="outlined" sx={{ mb: 2 }}>
        <CardContent>
          <Typography variant="subtitle2">① 分层形态（模型页给 worker 槽位选了模型）</Typography>
          <Typography variant="body2" color="text.secondary" component="div">
            主模型规划 + 子模型执行，支持派发多 agent。<br />
            生效配置：
            <ul style={{ margin: "4px 0", paddingLeft: 20 }}>
              <li>通用 · 默认步数上限 → 任务<b>总预算</b>（硬上限，耗尽即停）</li>
              <li>编排 · 派发最大轮次 / 并发 → 控制派发与回收</li>
              <li>编排 · 预算提示触发比例 → 接近预算时给 agent 陈述提示</li>
              <li>编排 · 分块步数 → SDK 单次内部步数块大小（检查点粒度）</li>
            </ul>
            <b>brain.long_task.* 在此路径不生效。</b>
          </Typography>
        </CardContent>
      </Card>

      <Card variant="outlined">
        <CardContent>
          <Typography variant="subtitle2">② 单主 agent 形态（worker 槽位未选模型时）</Typography>
          <Typography variant="body2" color="text.secondary" component="div">
            主模型自己跑完整任务，无子 agent。<br />
            生效配置：
            <ul style={{ margin: "4px 0", paddingLeft: 20 }}>
              <li>通用 · 默认步数上限 → 仍是<b>总预算</b>（硬上限）</li>
              <li>brain.long_task.enabled → 是否启用历史压缩</li>
              <li>brain.long_task.max_turns → 每 N 轮压缩一次历史（防上下文溢出，不停任务）</li>
              <li>brain.long_task.compress → 压缩时是否调用主模型生成中文摘要</li>
            </ul>
            <b>编排 · 派发轮次 / 并发在此形态仍生效</b>：派发由主模型自派发执行（并发与上下文隔离仍有价值）。
          </Typography>
        </CardContent>
      </Card>
    </Box>
  );
}

// 「伙伴」顶层 Tab：角色 / 画像 / 记忆 三个子 Tab（个人助手风格，收敛到设置）
function CompanionTab() {
  const [sub, setSub] = useState(0);
  return (
    <Box>
      <Tabs value={sub} onChange={(_, v) => setSub(v)} sx={{ mb: 2 }}>
        <Tab label="角色" />
        <Tab label="画像" />
        <Tab label="记忆" />
      </Tabs>
      {sub === 0 && <CharacterTab />}
      {sub === 1 && <ProfileTab />}
      {sub === 2 && <MemoryTab />}
    </Box>
  );
}

export default function Settings() {
  const [tab, setTab] = useState(0);
  return (
    <Box sx={{ p: 2, pt: 1.5, maxWidth: 960 }}>
      <Typography variant="subtitle1" gutterBottom sx={{ fontWeight: 600 }}>设置</Typography>
      <Tabs value={tab} onChange={(_, v) => setTab(v)} sx={{ mb: 2 }}>
        <Tab label="通用" />
        <Tab label="伙伴" />
        <Tab label="安全" />
        <Tab label="模型" />
        <Tab label="编排" />
        <Tab label="关于" />
      </Tabs>
      {tab === 0 && <GeneralTab />}
      {tab === 1 && <CompanionTab />}
      {tab === 2 && <SecurityTab />}
      {tab === 3 && <ModelTab />}
      {tab === 4 && <OrchestrationTab />}
      {tab === 5 && <AboutTab />}
    </Box>
  );
}
