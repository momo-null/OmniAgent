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
import { audioApi, runtimeApi } from "../../api/client";
import type { PluginConfigField, PluginConfigResponse, VoiceStatus } from "../../types";

/**
 * 语音引擎（设置 · 模型 → 本地模型 下方）。
 *
 * 服务化之后这里有两份配置，边界要分清：
 * - **调用端点（插件）**：`~/.omniagent/plugins/voice.yaml`——base_url / 密钥 / 模型名，
 *   换在线引擎只动这一块；
 * - **语音服务（引擎）**：`~/.omniagent/voice_service.yaml`——端口 / 空闲回收 / 音色 / 路径，
 *   属于独立服务，不属于插件目录。
 *
 * 字段全部来自插件自报 schema（`plugin.json` 的 `config` / `service_config`），
 * 本组件只按 type 渲染；唯一的语音知识是「音色下拉用 /healthz 报的 voices」。
 */

const isLocalUrl = (url: string) =>
  /^https?:\/\/(127\.0\.0\.1|localhost)(:\d+)?(\/|$)/i.test(url.trim());

interface Bag {
  fields: PluginConfigField[];
  values: Record<string, unknown>;
  secrets: Record<string, boolean>;
}

export default function VoiceEngineCard() {
  const [pluginBag, setPluginBag] = useState<Bag>({ fields: [], values: {}, secrets: {} });
  const [serviceBag, setServiceBag] = useState<Bag>({ fields: [], values: {}, secrets: {} });
  const [status, setStatus] = useState<VoiceStatus | null>(null);
  const [disabled, setDisabled] = useState(false);
  const [advanced, setAdvanced] = useState(false);
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");

  const load = async () => {
    try {
      const [p, s] = await Promise.all([
        runtimeApi.pluginConfig("voice", "plugin"),
        runtimeApi.pluginConfig("voice", "service"),
      ]);
      const pd = p.data as PluginConfigResponse;
      const sd = s.data as PluginConfigResponse;
      setPluginBag((b) => ({
        ...b, fields: pd.schema || [], values: pd.values || {},
        secrets: pd.secrets_set || {},
      }));
      setServiceBag((b) => ({
        ...b, fields: sd.schema || [], values: sd.values || {},
        secrets: sd.secrets_set || {},
      }));
      setDisabled(false);
    } catch (e) {
      setDisabled(true);
      setErr((e as Error).message);
    }
    try {
      setStatus(await audioApi.status());
    } catch {
      setStatus(null);
    }
  };

  useEffect(() => {
    void load();
  }, []);

  // patch 绑定到具体的 bag（保存时按 scope 各写各的文件）
  const patchOf = (setter: typeof setPluginBag) => (next: Record<string, unknown>) =>
    setter((b) => ({ ...b, values: { ...b.values, ...next } }));

  const save = async (scope: "plugin" | "service") => {
    const bag = scope === "plugin" ? pluginBag : serviceBag;
    setBusy(`save:${scope}`);
    setErr("");
    setMsg("");
    try {
      await runtimeApi.savePluginConfig("voice", bag.values, scope);
      // 服务端口改了而端点仍指向本机 → 同步端点，避免「改了端口却没生效」
      if (scope === "service") {
        const port = Number(bag.values.service_port);
        const base = String(pluginBag.values.base_url || "");
        if (Number.isFinite(port) && isLocalUrl(base)) {
          const fixed = `http://127.0.0.1:${port}/v1`;
          if (fixed !== base) {
            await runtimeApi.savePluginConfig("voice", { base_url: fixed }, "plugin");
            setPluginBag((b) => ({ ...b, values: { ...b.values, base_url: fixed } }));
          }
        }
      }
      setMsg(scope === "service" ? "服务配置已保存（重启服务后生效）" : "端点配置已保存（下次调用即生效）");
      await load();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy("");
    }
  };

  const preview = async () => {
    setBusy("tts");
    setErr("");
    setMsg("");
    try {
      const blob = await audioApi.tts("你好，语音引擎接上了，这是试听。");
      await new Audio(URL.createObjectURL(blob)).play();
      setMsg("试听成功：引擎按当前端点返回了音频");
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy("");
    }
  };

  // ── 通用控件（只认 type，不认键名） ──
  const control = (bag: Bag, key: string,
    patch: (next: Record<string, unknown>) => void,
    voiceOptions?: { value: string; label: string }[]) => {
    const f = bag.fields.find((x) => x.key === key);
    if (!f) return null;
    const v = bag.values[key];
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
        placeholder={f.type === "password" && bag.secrets[key] ? "已配置，留空保持不变" : ""}
        onChange={(e) => patch({ [key]: e.target.value })} />
    );
  };

  const detail = status?.service.detail ?? null;
  const voices = (detail?.voices || []).map((v) => ({ value: v, label: v }));
  const reachable = Boolean(status?.service.reachable);
  const busySave = (scope: string) => busy === `save:${scope}`;

  return (
    <Paper variant="outlined" sx={{ mb: 2, p: 2 }}>
      <Stack direction="row" justifyContent="space-between" alignItems="center">
        <Stack direction="row" spacing={1} alignItems="center">
          <Typography variant="subtitle2">语音引擎</Typography>
          {disabled ? (
            <Chip size="small" label="插件未启用" />
          ) : (
            <Chip size="small" color={reachable ? "success" : "default"}
              label={reachable ? "服务运行中" : "服务未运行"} />
          )}
        </Stack>
        <Button size="small" onClick={() => void load()}>刷新状态</Button>
      </Stack>

      <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.5 }}>
        {status
          ? `端点 ${status.endpoint.base_url || "（未配置）"} ｜ ` +
            (detail
              ? `STT ${detail.stt_loaded ? "已加载" : "按需加载"}${detail.models_ready ? "" : "（模型缺失）"} ｜ ` +
                `TTS ${detail.tts_running ? `运行中 pid ${detail.tts_pid}` : "按需加载"} ｜ ` +
                `音色 ${detail.voices.length} 个 ｜ 空闲释放 ${detail.idle_release_sec}s`
              : `服务不可达${status.service.error ? `（${status.service.error}）` : ""}`)
          : "状态不可读（插件未启用或后端未就绪）"}
      </Typography>

      {err && <Alert severity="error" sx={{ mt: 1 }} onClose={() => setErr("")}>{err}</Alert>}
      {msg && <Alert severity="success" sx={{ mt: 1 }} onClose={() => setMsg("")}>{msg}</Alert>}

      {!disabled && (
        <>
          <Divider sx={{ my: 1.5 }} />
          <Typography variant="body2" sx={{ fontWeight: 500 }}>调用端点</Typography>
          <Typography variant="caption" color="text.secondary">
            插件只做接口调用：换本机服务或在线厂商都只改这一块。
          </Typography>
          <Box sx={{ maxWidth: 560, display: "flex", flexDirection: "column", gap: 1.5, mt: 1 }}>
            {control(pluginBag, "base_url", patchOf(setPluginBag))}
            {control(pluginBag, "api_key", patchOf(setPluginBag))}
            {control(pluginBag, "stt_model", patchOf(setPluginBag))}
            {control(pluginBag, "tts_model", patchOf(setPluginBag))}
            {control(pluginBag, "voice", patchOf(setPluginBag), voices)}
            <Stack direction="row" spacing={1} alignItems="center">
              <Button size="small" variant="contained" disabled={busySave("plugin")}
                onClick={() => void save("plugin")}>
                {busySave("plugin") ? "保存中..." : "保存端点"}
              </Button>
              <Button size="small" variant="outlined" disabled={busy === "tts"}
                onClick={() => void preview()}>
                {busy === "tts" ? "合成中..." : "试听"}
              </Button>
            </Stack>
          </Box>

          <Divider sx={{ my: 1.5 }} />
          <Typography variant="body2" sx={{ fontWeight: 500 }}>语音服务（引擎）</Typography>
          <Typography variant="caption" color="text.secondary">
            独立常驻服务：没有请求时释放模型，进程不退；desktop 启动时按端口拉起。
          </Typography>
          <Box sx={{ maxWidth: 560, display: "flex", flexDirection: "column", gap: 1.5, mt: 1 }}>
            {control(serviceBag, "service_port", patchOf(setServiceBag))}
            {control(serviceBag, "tts_voice", patchOf(setServiceBag), voices)}
            {control(serviceBag, "service_idle_sec", patchOf(setServiceBag))}
            {control(serviceBag, "stt_idle_sec", patchOf(setServiceBag))}
            {control(serviceBag, "tts_idle_sec", patchOf(setServiceBag))}
            <Stack direction="row" spacing={1} alignItems="center">
              <Button size="small" variant="contained" disabled={busySave("service")}
                onClick={() => void save("service")}>
                {busySave("service") ? "保存中..." : "保存服务配置"}
              </Button>
              <Button size="small" onClick={() => setAdvanced((v) => !v)}>
                {advanced ? "收起高级" : "高级（地址 / 令牌 / 路径）"}
              </Button>
            </Stack>
            <Collapse in={advanced}>
              <Stack spacing={1.5} sx={{ pt: 1 }}>
                {control(serviceBag, "service_host", patchOf(setServiceBag))}
                {control(serviceBag, "service_token", patchOf(setServiceBag))}
                {control(serviceBag, "tts_eq", patchOf(setServiceBag))}
                {["stt_model_dir", "gptsovits_dir", "voices_dir", "gptsovits_repo",
                  "tts_python", "tts_vits", "tts_t2s"].map((k) => (
                  <Box key={k}>{control(serviceBag, k, patchOf(setServiceBag))}</Box>
                ))}
              </Stack>
            </Collapse>
          </Box>
        </>
      )}
    </Paper>
  );
}
