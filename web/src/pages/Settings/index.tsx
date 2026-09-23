import { useEffect, useState } from "react";
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

const DEFAULT_MODELS_DIR = "D:\\AI\\Models";

const buildBody = (p?: LaunchParams): Record<string, unknown> => {
  const b: Record<string, unknown> = {};
  if (!p) return b;
  if (p.gguf_path) b.gguf_path = p.gguf_path;
  if (p.threads != null) b.threads = p.threads;
  if (p.ctx_size != null) b.ctx_size = p.ctx_size;
  if (p.gpu_layers != null) b.gpu_layers = p.gpu_layers;
  if (p.port != null) b.port = p.port;
  if (p.reasoning_budget != null) b.reasoning_budget = p.reasoning_budget;
  if (p.use_mmproj != null) b.use_mmproj = p.use_mmproj;
  if (p.profile) b.profile = p.profile;
  return b;
};

const initDraft = (m: ModelInfo): LaunchParams => ({
  threads: m.threads, ctx_size: m.ctx_size, gpu_layers: m.gpu_layers,
  port: m.port, reasoning_budget: m.reasoning_budget, use_mmproj: null, profile: null,
});

const buildSaveBody = (m: ModelInfo, d: LaunchParams): Record<string, unknown> => {
  const b: Record<string, unknown> = { gguf_path: m.gguf_path };
  if (d.ctx_size != null) b.ctx_size = d.ctx_size;
  if (d.gpu_layers != null) b.gpu_layers = d.gpu_layers;
  if (d.threads != null) b.threads = d.threads;
  b.port = d.port ?? null;
  if (d.reasoning_budget != null) b.reasoning_budget = d.reasoning_budget;
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
      <FormControl size="small">
        <InputLabel id="mmproj-label">mmproj 多模态投影</InputLabel>
        <Select labelId="mmproj-label" label="mmproj 多模态投影"
          value={draft.use_mmproj === null ? "auto" : String(draft.use_mmproj)}
          onChange={(e) => { const v = e.target.value; setDraft({ ...draft, use_mmproj: v === "auto" ? null : v === "true" }); }}>
          <MenuItem value="auto">自动（存在则加）</MenuItem>
          <MenuItem value="true">强制开启</MenuItem>
          <MenuItem value="false">关闭</MenuItem>
        </Select>
      </FormControl>
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
  const [modelsDir, setModelsDir] = useState<string>(DEFAULT_MODELS_DIR);
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
      const [m, g] = await Promise.all([modelApi.list(modelsDir), systemApi.gpu()]);
      setModels(m.data as ModelInfo[]);
      setGpu((g.data as GpuInfo) ?? null);
    } catch (e) { setError((e as Error).message); }
  };

  useEffect(() => {
    setDrafts((prev) => { const next = { ...prev }; for (const m of models) if (!next[m.name]) next[m.name] = initDraft(m); return next; });
  }, [models]);

  // A：去掉 5s 自动轮询，界面保持安静；状态更新只在显式刷新 / 启停 / 校验后发生
  useEffect(() => { refresh(); }, []);

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
          <TextField label="模型目录（扫描根目录）" size="small" fullWidth value={modelsDir}
            onChange={(e) => setModelsDir(e.target.value)} onBlur={refresh} />
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
          label={<LabelWithTip text="启用长任务压缩 (brain.long_task.enabled)" desc="未配置子 agent 模型（executor）时，主模型单大脑长跑会按间隔压缩历史；关闭则不做压缩。" />} />
        <TextField label={<LabelWithTip text="压缩间隔 (brain.long_task.max_turns)" desc="单大脑路径下，每跑 N 轮把历史压缩成一段摘要，防止上下文溢出。用完不会停任务，只控制压缩频率。" />} type="number" value={ltMaxTurns}
          onChange={(e) => setLtMaxTurns(Number(e.target.value) || 16)} />
        <FormControlLabel control={<Switch checked={ltCompress} onChange={(e) => setLtCompress(e.target.checked)} />}
          label={<LabelWithTip text="调用主模型生成摘要 (brain.long_task.compress)" desc="开启：压缩时调用主模型生成中文摘要（更省上下文）。关闭：退化为截断兜底，不消耗额外调用。" />} />

        <Divider />
        <Typography variant="subtitle2" sx={{ mt: 1 }}>上下文治理（高级）</Typography>
        <TextField label={<LabelWithTip text="模型上下文上限 (brain.maxInputTokens)" desc="该模型的输入 Token 上限。填 0 = 关闭 Model 适配层粘性压缩；>0 时按此上限动态计算压缩阈值（前缀复用，省 token）。新 schema 请在「通道」页的 Provider 上配同名字段。" />} type="number" value={maxInputTokens}
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

interface ChannelCfg {
  enabled?: boolean;
  provider?: string;
  base_url?: string;
  model?: string;
  api_key?: string;
  api_key_env?: string;
  capabilities?: { vision?: boolean };
  request?: { temperature?: number; max_tokens?: number };
}

// M-new：模型 Provider（端点定义，可被多个 agent 引用）
interface ProviderCfg {
  provider?: string;
  base_url?: string;
  model?: string;
  api_key?: string;
  api_key_env?: string;
  capabilities?: { vision?: boolean };
  // 模型输入 Token 上限：0 = 关闭 Model 适配层粘性压缩
  maxInputTokens?: number;
}

// M-new：Agent 配置（model 字段引用某个 provider 的名称）
interface AgentCfg {
  enabled?: boolean;
  model?: string;
  tools?: string[];
  dispatchable?: string[];
}

// 通道/Provider/Agent 的右栏编辑表单（替代原折叠卡片 + Select 下拉）
function ChannelForm({ title, subtitle, cfg, onChange, canDisable }: {
  title: string; subtitle: string; cfg: ChannelCfg; onChange: (c: ChannelCfg) => void; canDisable?: boolean;
}) {
  const apiKeySet = (cfg as any).api_key_set as boolean | undefined;
  return (
    <Card><CardContent>
      <Typography variant="subtitle2">{title}</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>{subtitle}</Typography>
      <Stack spacing={2} sx={{ maxWidth: 480 }}>
        {canDisable && (
          <FormControlLabel control={<Switch checked={cfg.enabled !== false}
            onChange={(e) => onChange({ ...cfg, enabled: e.target.checked })} />} label="启用" />
        )}
        <TextField label="base_url" size="small" fullWidth value={cfg.base_url ?? ""}
          onChange={(e) => onChange({ ...cfg, base_url: e.target.value })} />
        <TextField label="model" size="small" fullWidth value={cfg.model ?? ""}
          onChange={(e) => onChange({ ...cfg, model: e.target.value })} />
        <TextField label="api_key（留空保持不变）" size="small" fullWidth type="password"
          placeholder={apiKeySet ? "（已设置，留空保持不变）" : "（未设置）"}
          value={cfg.api_key ?? ""} onChange={(e) => onChange({ ...cfg, api_key: e.target.value })} />
        {cfg.capabilities && (
          <FormControlLabel control={<Switch checked={!!cfg.capabilities?.vision}
            onChange={(e) => onChange({ ...cfg, capabilities: { vision: e.target.checked } })} />} label="视觉能力" />
        )}
      </Stack>
    </CardContent></Card>
  );
}

function ProviderForm({ name, cfg, onChange, onRemove }: {
  name: string; cfg: ProviderCfg; onChange: (c: ProviderCfg) => void; onRemove: () => void;
}) {
  const apiKeySet = (cfg as any).api_key_set as boolean | undefined;
  return (
    <Card><CardContent>
      <Stack direction="row" justifyContent="space-between" alignItems="center">
        <Typography variant="subtitle2">Provider：{name || "(未命名)"}</Typography>
        <Button size="small" color="error" onClick={onRemove}>删除</Button>
      </Stack>
      <Stack spacing={2} sx={{ mt: 1, maxWidth: 480 }}>
        <TextField label="base_url" size="small" fullWidth value={cfg.base_url ?? ""}
          onChange={(e) => onChange({ ...cfg, base_url: e.target.value })} />
        <TextField label="model" size="small" fullWidth value={cfg.model ?? ""}
          onChange={(e) => onChange({ ...cfg, model: e.target.value })} />
        <TextField label="api_key（留空保持不变）" size="small" fullWidth type="password"
          placeholder={apiKeySet ? "（已设置，留空保持不变）" : "（未设置）"}
          value={cfg.api_key ?? ""} onChange={(e) => onChange({ ...cfg, api_key: e.target.value })} />
        <TextField label="api_key_env" size="small" fullWidth value={cfg.api_key_env ?? ""}
          onChange={(e) => onChange({ ...cfg, api_key_env: e.target.value })} />
        <TextField label={<LabelWithTip text="上下文上限 maxInputTokens（0=关闭）" desc="该模型的输入 Token 上限。填 0 关闭 Model 适配层粘性压缩；>0 时启用粘性增量压缩（请求前缀复用，省 token 且避免上下文溢出）。" />} type="number" size="small" fullWidth value={cfg.maxInputTokens ?? 0}
          onChange={(e) => onChange({ ...cfg, maxInputTokens: Number(e.target.value) || 0 })} />
        <FormControlLabel control={<Switch checked={!!cfg.capabilities?.vision}
          onChange={(e) => onChange({ ...cfg, capabilities: { vision: e.target.checked } })} />} label="视觉能力" />
      </Stack>
    </CardContent></Card>
  );
}

function AgentForm({ name, cfg, onChange, onRemove, providerNames }: {
  name: string; cfg: AgentCfg; onChange: (c: AgentCfg) => void; onRemove: () => void; providerNames: string[];
}) {
  const toolsText = (cfg.tools || []).join(", ");
  const dispatchText = (cfg.dispatchable || []).join(", ");
  return (
    <Card><CardContent>
      <Stack direction="row" justifyContent="space-between" alignItems="center">
        <Typography variant="subtitle2">Agent：{name}</Typography>
        <Button size="small" color="error" onClick={onRemove}>删除</Button>
      </Stack>
      <Stack spacing={2} sx={{ mt: 1, maxWidth: 480 }}>
        <FormControlLabel control={<Switch checked={cfg.enabled !== false}
          onChange={(e) => onChange({ ...cfg, enabled: e.target.checked })} />} label="启用" />
        <FormControl size="small" fullWidth>
          <InputLabel id={`${name}-model`}>model（引用 provider）</InputLabel>
          <Select labelId={`${name}-model`} label="model（引用 provider）" value={cfg.model ?? ""}
            onChange={(e) => onChange({ ...cfg, model: e.target.value })}>
            {providerNames.length === 0
              ? <MenuItem value=""><em>请先在上方添加 provider</em></MenuItem>
              : providerNames.map((n) => <MenuItem key={n} value={n}>{n}</MenuItem>)}
          </Select>
        </FormControl>
        <TextField label="tools（逗号分隔）" size="small" fullWidth value={toolsText}
          onChange={(e) => onChange({ ...cfg, tools: e.target.value.split(",").map((s) => s.trim()).filter(Boolean) })} />
        <TextField label="dispatchable（逗号分隔，可派发的子 agent）" size="small" fullWidth value={dispatchText}
          onChange={(e) => onChange({ ...cfg, dispatchable: e.target.value.split(",").map((s) => s.trim()).filter(Boolean) })} />
      </Stack>
    </CardContent></Card>
  );
}

type SelKey = { kind: "provider" | "agent" | "channel"; name: string };

function ChannelsTab() {
  const [brain, setBrain] = useState<ChannelCfg>({});
  const [executor, setExecutor] = useState<ChannelCfg>({ enabled: true });
  const [vision, setVision] = useState<ChannelCfg>({ enabled: true });
  const [localAsTool, setLocalAsTool] = useState<ChannelCfg>({ enabled: true });
  const [providers, setProviders] = useState<Record<string, ProviderCfg>>({});
  const [agents, setAgents] = useState<Record<string, AgentCfg>>({});
  const [saved, setSaved] = useState("");
  const [error, setError] = useState("");
  const [sel, setSel] = useState<SelKey | null>({ kind: "channel", name: "brain" });
  useEffect(() => {
    settingsApi.get().then((r) => {
      const d = r.data as any;
      // 三通道单一真源：brain 顶层，executor/vision 嵌套在 runtime 下
      if (d?.brain) setBrain(d.brain);
      const rt = d?.runtime || {};
      if (rt.executor) setExecutor(rt.executor);
      if (rt.vision) setVision(rt.vision);
      if (rt.agents) setAgents(rt.agents);
      if (d?.llm?.local_as_tool) setLocalAsTool(d.llm.local_as_tool);
      if (d?.llm?.providers) setProviders(d.llm.providers);
    }).catch(() => {});
  }, []);
  const providerNames = Object.keys(providers);
  const agentNames = Object.keys(agents);
  const handleSave = async () => {
    setError(""); setSaved("");
    try {
      // 旧通道（brain / executor / vision / local_as_tool）与新 schema（providers / agents）一并提交；
      // 内核在有 runtime.agents 时优先用新 schema，否则回退旧通道。
      const patch: Record<string, unknown> = {
        brain,
        runtime: { executor, vision, agents },
        llm: { local_as_tool: localAsTool, providers },
      };
      const r = await settingsApi.put(patch);
      setSaved((r.data as any)?.ok ? "已保存通道配置" : "保存失败");
    } catch (e) { setError((e as Error).message); }
  };
  const addProvider = () => {
    const name = window.prompt("新 Provider 名称（如 online / local）：");
    if (!name) return;
    const key = name.trim();
    if (!key || providers[key]) return;
    setProviders({ ...providers, [key]: { provider: "openai-compatible", base_url: "", model: "" } });
    setSel({ kind: "provider", name: key });
  };
  const addAgent = () => {
    const name = window.prompt("新 Agent 名称（如 main / worker）：");
    if (!name) return;
    const key = name.trim();
    if (!key || agents[key]) return;
    setAgents({ ...agents, [key]: { enabled: true, model: providerNames[0] || "", tools: [], dispatchable: [] } });
    setSel({ kind: "agent", name: key });
  };
  const removeProvider = (name: string) => {
    const n = { ...providers }; delete n[name]; setProviders(n);
    if (sel?.kind === "provider" && sel.name === name) setSel(null);
  };
  const removeAgent = (name: string) => {
    const n = { ...agents }; delete n[name]; setAgents(n);
    if (sel?.kind === "agent" && sel.name === name) setSel(null);
  };

  const OLD_CHANNELS: { key: string; title: string; subtitle: string; cfg: ChannelCfg; set: (c: ChannelCfg) => void; canDisable: boolean }[] = [
    { key: "brain", title: "主模型", subtitle: "主 agent（默认自己跑完整任务）", cfg: brain, set: setBrain, canDisable: false },
    { key: "executor", title: "子 agent 模型", subtitle: "主 agent 派发子任务时用的模型（可配本地高频模型）", cfg: executor, set: setExecutor, canDisable: true },
    { key: "vision", title: "视觉（vision）", subtitle: "本地 VLM 视觉工具", cfg: vision, set: setVision, canDisable: true },
    { key: "local_as_tool", title: "本地模型（工具）", subtitle: "以 local_infer 工具暴露，由主模型决定是否派发", cfg: localAsTool, set: setLocalAsTool, canDisable: true },
  ];

  return (
    <Box>
      <Typography variant="subtitle2" gutterBottom>推理通道（多模型接入）</Typography>
      <Alert severity="info" sx={{ mb: 2 }}>
        各通道独立可配模型端点。「主模型」可以是在线模型，也可以直接填本地端点；
        「本地模型（工具）」开启后，本地模型会作为一个工具交给主模型自行判断是否派发。
      </Alert>

      <Stack direction="row" spacing={1} sx={{ mb: 2 }}>
        <Button variant="outlined" onClick={addProvider}>添加 Provider</Button>
        <Button variant="outlined" onClick={addAgent}>添加 Agent</Button>
        <Button variant="contained" onClick={handleSave} sx={{ ml: "auto" }}>保存</Button>
      </Stack>

      <Box sx={{ display: "flex", gap: 2, alignItems: "flex-start" }}>
        <Paper variant="outlined" sx={{ width: 300, flexShrink: 0, maxHeight: "70vh", overflow: "auto" }}>
          <List dense>
            {providerNames.length > 0 && <ListSubheader>Providers</ListSubheader>}
            {providerNames.map((name) => (
              <ListItemButton key={`p-${name}`} selected={sel?.kind === "provider" && sel.name === name}
                onClick={() => setSel({ kind: "provider", name })}>
                <ListItemText primary={name} secondary={`${providers[name].model || "—"} @ ${providers[name].base_url || "—"}`} />
              </ListItemButton>
            ))}
            {agentNames.length > 0 && <ListSubheader>Agents</ListSubheader>}
            {agentNames.map((name) => (
              <ListItemButton key={`a-${name}`} selected={sel?.kind === "agent" && sel.name === name}
                onClick={() => setSel({ kind: "agent", name })}>
                <ListItemText primary={name} secondary={`model: ${agents[name].model || "—"}`} />
              </ListItemButton>
            ))}
            <ListSubheader>旧通道（兼容）</ListSubheader>
            {OLD_CHANNELS.map((c) => (
              <ListItemButton key={`c-${c.key}`} selected={sel?.kind === "channel" && sel.name === c.key}
                onClick={() => setSel({ kind: "channel", name: c.key })}>
                <ListItemText primary={c.title} secondary={c.canDisable ? (c.cfg.enabled === false ? "已关闭" : "启用") : "常驻"} />
              </ListItemButton>
            ))}
          </List>
        </Paper>
        <Box sx={{ flex: 1, minWidth: 0 }}>
          {sel == null && <Alert severity="info">选择左侧配置项以编辑</Alert>}
          {sel?.kind === "provider" && providers[sel.name] && (
            <ProviderForm name={sel.name} cfg={providers[sel.name]} onChange={(c) => setProviders({ ...providers, [sel.name]: c })} onRemove={() => removeProvider(sel.name)} />
          )}
          {sel?.kind === "agent" && agents[sel.name] && (
            <AgentForm name={sel.name} cfg={agents[sel.name]} providerNames={providerNames}
              onChange={(c) => setAgents({ ...agents, [sel.name]: c })} onRemove={() => removeAgent(sel.name)} />
          )}
          {sel?.kind === "channel" && (() => {
            const c = OLD_CHANNELS.find((x) => x.key === sel!.name);
            if (!c) return null;
            return <ChannelForm title={c.title} subtitle={c.subtitle} cfg={c.cfg} canDisable={c.canDisable} onChange={c.set} />;
          })()}
        </Box>
      </Box>
      {saved && <Alert severity="success" sx={{ mt: 2 }}>{saved}</Alert>}
      {error && <Alert severity="error" sx={{ mt: 2 }}>{error}</Alert>}
    </Box>
  );
}

function AboutTab() {
  return (
    <Box>
      <Typography variant="subtitle2" gutterBottom>关于</Typography>
      <Typography variant="body2" color="text.secondary">
        OmniAgent · 通用 agent 编排内核（Plan C：project + task 两实体）。<br />
        大脑（在线强模型）作规划/反思，本地快模型作执行器，知识库自学（world-model / skill）。<br />
        内核零场景硬编码；能力来自工具与运行时自学。
      </Typography>

      <Typography variant="subtitle2" sx={{ mt: 3 }} gutterBottom>执行路径与配置生效范围</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
        引擎有两条执行路径，同一份配置里只有部分键在对应路径生效——别混：
      </Typography>

      <Card variant="outlined" sx={{ mb: 2 }}>
        <CardContent>
          <Typography variant="subtitle2">① 双层路径（配了 executor / runtime.agents）</Typography>
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
          <Typography variant="subtitle2">② 单大脑路径（未配 executor，不想启本地模型时）</Typography>
          <Typography variant="body2" color="text.secondary" component="div">
            主模型自己跑完整任务，无子 agent。<br />
            生效配置：
            <ul style={{ margin: "4px 0", paddingLeft: 20 }}>
              <li>通用 · 默认步数上限 → 仍是<b>总预算</b>（硬上限）</li>
              <li>brain.long_task.enabled → 是否启用历史压缩</li>
              <li>brain.long_task.max_turns → 每 N 轮压缩一次历史（防上下文溢出，不停任务）</li>
              <li>brain.long_task.compress → 压缩时是否调用主模型生成中文摘要</li>
            </ul>
            <b>编排 · 派发轮次 / 并发在此路径无意义</b>（没有子 agent 可派）。
          </Typography>
        </CardContent>
      </Card>
    </Box>
  );
}

export default function Settings() {
  const [tab, setTab] = useState(0);
  return (
    <Box>
      <Typography variant="subtitle1" gutterBottom sx={{ fontWeight: 600 }}>设置</Typography>
      <Tabs value={tab} onChange={(_, v) => setTab(v)} sx={{ mb: 2 }}>
        <Tab label="通用" />
        <Tab label="模型" />
        <Tab label="通道" />
        <Tab label="编排" />
        <Tab label="关于" />
      </Tabs>
      {tab === 0 && <GeneralTab />}
      {tab === 1 && <ModelTab />}
      {tab === 2 && <ChannelsTab />}
      {tab === 3 && <OrchestrationTab />}
      {tab === 4 && <AboutTab />}
    </Box>
  );
}
