import { useEffect, useState } from "react";
import {
  Alert,
  Box,
  Button,
  Chip,
  Divider,
  Paper,
  Stack,
  Switch,
  TextField,
  Typography,
} from "@mui/material";
import { modelApi, settingsApi } from "../../api/client";

/**
 * 向量服务（设置 · 模型 → 语音引擎 下方）。
 *
 * 与语音同形态，两层开关必须分清：
 * - **调用端点（core）**：`~/.omniagent/config.yaml` 的
 *   `runtime.knowledge.memory.vector`——是否启用向量检索、端点、向量标识、语义门槛。
 *   换成线上向量服务只改 `service_url`。
 * - **服务启动（资源层）**：`~/.omniagent/model_hub.yaml` 的 `services.embed-svc`——
 *   本机 sidecar 起不起、是否随后端启动。
 *
 * 所以会出现「服务在跑但检索没启用」或反过来，状态行里要能看出来。
 */

interface VectorCore {
  enabled: boolean;
  service_url: string;
  model: string;
  cos_floor: number;
}

interface ServiceStatus {
  name: string;
  enabled: boolean;
  autostart: boolean;
  running: boolean;
  ready: boolean;
  port: number;
}

interface SettingsResponse {
  runtime?: { knowledge?: { memory?: { vector?: Partial<VectorCore> } } };
}

const DEFAULT_CORE: VectorCore = {
  enabled: true,
  service_url: "http://127.0.0.1:18743",
  model: "bge-m3",
  cos_floor: 0.52,
};

export default function VectorEngineCard() {
  const [core, setCore] = useState<VectorCore>(DEFAULT_CORE);
  const [svc, setSvc] = useState<ServiceStatus | null>(null);
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");

  const load = async () => {
    try {
      const [s, r] = await Promise.all([settingsApi.get(), modelApi.services()]);
      const v = (s.data as SettingsResponse)?.runtime?.knowledge?.memory?.vector ?? {};
      setCore({
        enabled: v.enabled !== false,
        service_url: String(v.service_url || DEFAULT_CORE.service_url),
        model: String(v.model || DEFAULT_CORE.model),
        cos_floor: Number(v.cos_floor ?? DEFAULT_CORE.cos_floor),
      });
      const list = (r.data as ServiceStatus[]) || [];
      setSvc(list.find((x) => x.name === "embed-svc") ?? null);
      setErr("");
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  useEffect(() => {
    void load();
  }, []);

  const act = async (label: string, fn: () => Promise<unknown>, done: string) => {
    setBusy(label);
    setErr("");
    setMsg("");
    try {
      await fn();
      await load();
      setMsg(done);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy("");
    }
  };

  const saveCore = () => act("core", () => settingsApi.put({
    runtime: {
      knowledge: {
        memory: {
          vector: {
            enabled: core.enabled,
            service_url: core.service_url.trim(),
            model: core.model.trim(),
            cos_floor: Number(core.cos_floor),
          },
        },
      },
    },
  }), "端点和检索开关已保存（下次运行生效）");

  const patchCore = (next: Partial<VectorCore>) => setCore((c) => ({ ...c, ...next }));

  const patchSvc = (next: Record<string, unknown>, done: string) =>
    act("svc", () => modelApi.updateService("embed-svc", next), done);

  const reachable = Boolean(svc?.ready);

  return (
    <Paper variant="outlined" sx={{ mb: 2, p: 2 }}>
      <Stack direction="row" justifyContent="space-between" alignItems="center">
        <Stack direction="row" spacing={1} alignItems="center">
          <Typography variant="subtitle2">向量服务（引擎）</Typography>
          <Chip size="small" color={reachable ? "success" : "default"}
            label={reachable ? "服务运行中" : "服务未运行"} />
          {svc && !core.enabled && (
            <Chip size="small" color="warning" label="运行中 · 检索未启用" />
          )}
        </Stack>
        <Button size="small" onClick={() => void load()}>刷新状态</Button>
      </Stack>

      <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.5 }}>
        {svc
          ? `端点 ${core.service_url || "（未配置）"} ｜ 端口 ${svc.port || "—"} ｜ ` +
            `向量标识 ${core.model || "—"} ｜ 语义门槛 ${core.cos_floor}`
          : "状态不可读（后端未就绪）"}
      </Typography>

      {err && <Alert severity="error" sx={{ mt: 1 }} onClose={() => setErr("")}>{err}</Alert>}
      {msg && <Alert severity="success" sx={{ mt: 1 }} onClose={() => setMsg("")}>{msg}</Alert>}

      <Divider sx={{ my: 1.5 }} />
      <Typography variant="body2" sx={{ fontWeight: 500 }}>调用端点（core）</Typography>
      <Typography variant="caption" color="text.secondary">
        内核只用地址：换本机服务或线上向量库都只改这一块，与下方「起不起服务」无关。
      </Typography>
      <Box sx={{ maxWidth: 560, display: "flex", flexDirection: "column", gap: 1.5, mt: 1 }}>
        <Stack direction="row" spacing={1} alignItems="center">
          <Switch size="small" checked={core.enabled}
            onChange={(e) => patchCore({ enabled: e.target.checked })} />
          <Typography variant="body2">启用向量检索（关闭则只用字面检索）</Typography>
        </Stack>
        <TextField size="small" fullWidth label="服务地址" value={core.service_url}
          helperText="本机 sidecar 或线上 OpenAI 兼容的 /embed + /health 服务"
          onChange={(e) => patchCore({ service_url: e.target.value })} />
        <Stack direction="row" spacing={1.5}>
          <TextField size="small" fullWidth label="向量标识" value={core.model}
            helperText="换模型时改这里，向量表按它区分版本"
            onChange={(e) => patchCore({ model: e.target.value })} />
          <TextField size="small" fullWidth type="number" label="语义门槛"
            value={core.cos_floor}
            helperText="余弦下限，越高越严格"
            onChange={(e) => patchCore({ cos_floor: Number(e.target.value) })} />
        </Stack>
        <Stack direction="row" spacing={1}>
          <Button size="small" variant="contained" disabled={busy === "core"}
            onClick={() => void saveCore()}>
            {busy === "core" ? "保存中..." : "保存端点配置"}
          </Button>
        </Stack>
      </Box>

      <Divider sx={{ my: 1.5 }} />
      <Typography variant="body2" sx={{ fontWeight: 500 }}>服务启动（资源层）</Typography>
      <Typography variant="caption" color="text.secondary">
        独立常驻服务：不加载模型也能活着，加载完常驻；后端启动时按开关拉起，端口占用视为已有实例。
      </Typography>
      {svc && (
        <Box sx={{ mt: 1, display: "flex", flexDirection: "column", gap: 1 }}>
          <Stack direction="row" spacing={2} alignItems="center" flexWrap="wrap">
            <Stack direction="row" spacing={0.5} alignItems="center">
              <Switch size="small" checked={svc.enabled}
                onChange={(e) => void patchSvc({ enabled: e.target.checked },
                  e.target.checked ? "服务已启用" : "服务已停用")} />
              <Typography variant="caption">启用服务</Typography>
            </Stack>
            <Stack direction="row" spacing={0.5} alignItems="center">
              <Switch size="small" checked={svc.autostart}
                onChange={(e) => void patchSvc({ autostart: e.target.checked },
                  "启动策略已更新")} />
              <Typography variant="caption">随后端启动</Typography>
            </Stack>
            <Box sx={{ flex: 1 }} />
            {svc.running ? (
              <Button size="small" disabled={busy === "stop"}
                onClick={() => void act("stop", () => modelApi.stopService("embed-svc"), "已停止")}>
                停止
              </Button>
            ) : (
              <Button size="small" variant="contained" disabled={busy === "start" || !svc.enabled}
                onClick={() => void act("start", () => modelApi.startService("embed-svc"), "已启动")}>
                启动
              </Button>
            )}
          </Stack>
        </Box>
      )}
    </Paper>
  );
}
