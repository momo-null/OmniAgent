import { useEffect, useState } from "react";
import {
  Alert,
  Box,
  Button,
  Chip,
  Collapse,
  Divider,
  MenuItem,
  Paper,
  Stack,
  Switch,
  TextField,
  Typography,
} from "@mui/material";
import { modelApi, runtimeApi } from "../../api/client";
import type { PluginConfigField, PluginConfigResponse } from "../../types";

/**
 * 语音服务（设置 · 模型 → 本地模型 下方）。
 *
 * 这里只维护 model_hub 的语音引擎服务：`~/.omniagent/voice_service.yaml` 的空闲回收 /
 * 音色 / 路径，以及 `~/.omniagent/model_hub.yaml` 的 `services.voice` 开关。
 * 插件调用端点（base_url / API Key / 模型名，以及试听）已移到
 * 右侧「语音调用端点」插件页（在技能与工具里启用语音插件后由 manifest 注入）；
 * 两层配置不再混在一张卡片里。
 *
 * 音色来自 model_hub 服务 `/healthz` 的 `detail.voices`，不依赖 voice 插件是否启用。
 */

interface Bag {
  fields: PluginConfigField[];
  values: Record<string, unknown>;
  secrets: Record<string, boolean>;
}

interface VoiceHealthDetail {
  voices?: string[];
  stt_loaded?: boolean;
  tts_running?: boolean;
  tts_pid?: number | null;
  models_ready?: boolean;
  idle_release_sec?: number;
}

interface VoiceServiceStatus {
  name: string;
  running: boolean;
  ready: boolean;
  port: number;
  detail?: VoiceHealthDetail | null;
}

export default function VoiceEngineCard() {
  const [serviceBag, setServiceBag] = useState<Bag>({ fields: [], values: {}, secrets: {} });
  const [service, setService] = useState<VoiceServiceStatus | null>(null);
  const [disabled, setDisabled] = useState(false);
  const [advanced, setAdvanced] = useState(false);
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");

  const load = async () => {
    try {
      const s = await runtimeApi.pluginConfig("voice", "service");
      const sd = s.data as PluginConfigResponse;
      setServiceBag({
        fields: sd.schema || [], values: sd.values || {}, secrets: sd.secrets_set || {},
      });
      setDisabled(false);
    } catch (e) {
      setDisabled(true);
      setErr((e as Error).message);
    }
    try {
      const r = await modelApi.services();
      const list = (r.data as VoiceServiceStatus[]) || [];
      setService(list.find((x) => x.name === "voice") ?? null);
    } catch {
      setService(null);
    }
  };

  useEffect(() => {
    void load();
  }, []);

  const patch = (next: Record<string, unknown>) =>
    setServiceBag((b) => ({ ...b, values: { ...b.values, ...next } }));

  const save = async () => {
    setBusy("save");
    setErr("");
    setMsg("");
    try {
      await runtimeApi.savePluginConfig("voice", serviceBag.values, "service");
      setMsg("服务配置已保存（重启语音服务后生效）");
      await load();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy("");
    }
  };

  // ── 通用控件（只认 schema 的 type，不认具体键名） ──
  const control = (key: string, voiceOptions?: { value: string; label: string }[]) => {
    const f = serviceBag.fields.find((x) => x.key === key);
    if (!f) return null;
    const v = serviceBag.values[key];
    const common = { size: "small" as const, fullWidth: true, label: f.label, helperText: f.desc };
    if (f.type === "bool") {
      return (
        <Stack direction="row" spacing={1} alignItems="center">
          <Switch size="small" checked={Boolean(v)}
            onChange={(e) => patch({ [key]: e.target.checked })} />
          <Typography variant="body2">{f.label}</Typography>
        </Stack>
      );
    }
    if (f.type === "number") {
      return (
        <TextField {...common} type="number" value={v ?? ""}
          onChange={(e) => patch({ [key]: e.target.value === "" ? "" : Number(e.target.value) })} />
      );
    }
    if (voiceOptions) {
      return (
        <TextField {...common} select value={String(v ?? "")}
          SelectProps={{ displayEmpty: true }}
          onChange={(e) => patch({ [key]: e.target.value })}>
          <MenuItem value="">（默认：第一个可用音色）</MenuItem>
          {voiceOptions.map((o) => <MenuItem key={o.value} value={o.value}>{o.label}</MenuItem>)}
        </TextField>
      );
    }
    if (f.type === "select") {
      return (
        <TextField {...common} select value={String(v ?? "")}
          onChange={(e) => patch({ [key]: e.target.value })}>
          {(f.options || []).map((o) => <MenuItem key={o.value} value={o.value}>{o.label}</MenuItem>)}
        </TextField>
      );
    }
    return (
      <TextField {...common} type={f.type === "password" ? "password" : "text"}
        value={String(v ?? "")}
        placeholder={f.type === "password" && serviceBag.secrets[key] ? "已配置，留空保持不变" : ""}
        onChange={(e) => patch({ [key]: e.target.value })} />
    );
  };

  const detail = service?.detail ?? null;
  const voices = (detail?.voices || []).map((v) => ({ value: v, label: v }));
  const currentVoice = String(serviceBag.values.tts_voice ?? "");
  const voiceOptions = voices.length
    ? voices
    : currentVoice ? [{ value: currentVoice, label: currentVoice }] : [];
  const reachable = Boolean(service?.ready);

  return (
    <Paper variant="outlined" sx={{ mb: 2, p: 2 }}>
      <Stack direction="row" justifyContent="space-between" alignItems="center">
        <Stack direction="row" spacing={1} alignItems="center">
          <Typography variant="subtitle2">语音服务（引擎）</Typography>
          <Chip size="small" color={reachable ? "success" : "default"}
            label={reachable ? "服务运行中" : "服务未运行"} />
        </Stack>
        <Button size="small" onClick={() => void load()}>刷新状态</Button>
      </Stack>

      <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.5 }}>
        {service
          ? `端口 ${service.port || "—"} ｜ ` +
            (detail
              ? `STT ${detail.stt_loaded ? "已加载" : "按需加载"}${detail.models_ready === false ? "（模型缺失）" : ""} ｜ ` +
                `TTS ${detail.tts_running ? `运行中 pid ${detail.tts_pid ?? "—"}` : "按需加载"} ｜ ` +
                `音色 ${detail.voices?.length ?? 0} 个 ｜ 空闲释放 ${detail.idle_release_sec ?? "—"}s`
              : "健康详情不可读")
          : "状态不可读（后端未就绪）"}
      </Typography>

      {err && <Alert severity="error" sx={{ mt: 1 }} onClose={() => setErr("")}>{err}</Alert>}
      {msg && <Alert severity="success" sx={{ mt: 1 }} onClose={() => setMsg("")}>{msg}</Alert>}

      {!disabled && (
        <>
          <Divider sx={{ my: 1.5 }} />
          <Typography variant="body2" sx={{ fontWeight: 500 }}>引擎参数</Typography>
          <Typography variant="caption" color="text.secondary">
            独立常驻服务：没有请求时释放模型，进程不退；端口与启停归 model_hub 清单。
            调用端点和试听在右侧「语音调用端点」插件页里。
          </Typography>
          <Box sx={{ maxWidth: 560, display: "flex", flexDirection: "column", gap: 1.5, mt: 1 }}>
            {control("tts_voice", voiceOptions)}
            {control("service_idle_sec")}
            {control("stt_idle_sec")}
            {control("tts_idle_sec")}
            <Stack direction="row" spacing={1} alignItems="center">
              <Button size="small" variant="contained" disabled={busy === "save"}
                onClick={() => void save()}>
                {busy === "save" ? "保存中..." : "保存服务配置"}
              </Button>
              <Button size="small" onClick={() => setAdvanced((v) => !v)}>
                {advanced ? "收起高级" : "高级（地址 / 令牌 / 路径）"}
              </Button>
            </Stack>
            <Collapse in={advanced}>
              <Stack spacing={1.5} sx={{ pt: 1 }}>
                {control("service_host")}
                {control("service_token")}
                {control("tts_eq")}
                {["stt_model_dir", "gptsovits_dir", "voices_dir", "gptsovits_repo",
                  "tts_python", "tts_vits", "tts_t2s"].map((k) => (
                  <Box key={k}>{control(k)}</Box>
                ))}
              </Stack>
            </Collapse>
          </Box>
        </>
      )}
    </Paper>
  );
}
