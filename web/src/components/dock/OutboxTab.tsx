// B站待发审批卡（大肥鱼自运营 M3）：主人批准 / 作废她的待出站内容。
// 逻辑全部走后端 /bilibili/outbox*（backend/api/routers/bilibili_api.py）——写路径
// 单点收口在通道适配器（全量四道闸），这里只是批准入口的换壳。
import { useCallback, useEffect, useState } from "react";
import axios from "axios";
import {
  Box,
  Button,
  Chip,
  Stack,
  Typography,
} from "@mui/material";

const http = axios.create({ baseURL: "", timeout: 30000 });

interface OutboxItem {
  no: number;
  ts: number;
  status: string;
  kind: string;
  chat_id: string;
  text: string;
}

interface OutboxResp {
  ok: boolean;
  items: OutboxItem[];
  mode: string;
  ready: boolean;
  quotas: Record<string, number>;
  state: Record<string, unknown>;
  error?: string;
}

const statusChip = (s: string) => {
  const map: Record<string, { label: string; color: "default" | "success" | "warning" | "error" }> = {
    pending: { label: "待批", color: "warning" },
    sent: { label: "已发", color: "success" },
    discarded: { label: "已作废", color: "error" },
  };
  const cfg = map[s] ?? { label: s, color: "default" as const };
  return <Chip size="small" label={cfg.label} color={cfg.color} variant="outlined" />;
};

export default function OutboxTab() {
  const [resp, setResp] = useState<OutboxResp | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setErr("");
      const r = await http.get<OutboxResp>("/bilibili/outbox");
      setResp(r.data);
    } catch (e) {
      setErr(axios.isAxiosError(e) ? String(e.response?.data?.error ?? e.message) : String(e));
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const act = async (no: number, action: "approve" | "discard") => {
    setBusy(true);
    try {
      await http.post(`/bilibili/outbox/${no}/${action}`);
      await load();
    } catch (e) {
      setErr(axios.isAxiosError(e) ? String(e.response?.data?.error ?? e.message) : String(e));
    } finally {
      setBusy(false);
    }
  };

  const pend = (resp?.items ?? []).filter((i) => i.status === "pending");
  const done = (resp?.items ?? []).filter((i) => i.status !== "pending");

  return (
    <Box sx={{ height: "100%", overflow: "auto", p: 1.5, display: "flex", flexDirection: "column", gap: 1.5 }}>
      <Stack direction="row" alignItems="center" spacing={1}>
        <Typography variant="subtitle2">B站待发审批</Typography>
        <Chip size="small" label={resp ? (resp.ready ? `已登录 · ${resp.mode}` : "未登录") : "…"}
              color={resp?.ready ? "success" : "default"} variant="outlined" />
        {resp && <Chip size="small" variant="outlined"
                       label={`今日 ${resp.state?.comment ?? 0}/${resp.quotas?.comment ?? 0} 评 · ${resp.state?.dynamic ?? 0}/${resp.quotas?.dynamic ?? 0} 动`} />}
        <Button size="small" onClick={() => void load()} sx={{ ml: "auto" }}>刷新</Button>
      </Stack>
      {err && <Typography variant="caption" color="error">{err}</Typography>}
      {pend.length === 0 && <Typography variant="body2" color="text.secondary">待发队列为空——她觉醒产出的内容会先进这里等你批准。</Typography>}
      {pend.map((i) => (
        <Box key={i.no} sx={{ border: 1, borderColor: "divider", borderRadius: 1, p: 1.25 }}>
          <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 0.5 }}>
            {statusChip(i.status)}
            <Typography variant="caption" color="text.secondary">
              #{i.no} · {i.kind === "dynamic" ? "动态" : i.kind === "comment" ? "评论" : i.kind} · {i.chat_id}
            </Typography>
          </Stack>
          <Typography variant="body2" sx={{ whiteSpace: "pre-wrap", mb: 1 }}>{i.text}</Typography>
          <Stack direction="row" spacing={1}>
            <Button size="small" variant="contained" disabled={busy} onClick={() => void act(i.no, "approve")}>批准发送</Button>
            <Button size="small" color="error" disabled={busy} onClick={() => void act(i.no, "discard")}>作废</Button>
          </Stack>
        </Box>
      ))}
      {done.length > 0 && (
        <>
          <Typography variant="caption" color="text.secondary">历史（已发/已作废）</Typography>
          {done.slice(0, 20).map((i) => (
            <Stack key={i.no} direction="row" spacing={1} alignItems="center" sx={{ opacity: 0.7 }}>
              {statusChip(i.status)}
              <Typography variant="caption" sx={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                #{i.no} {i.text}
              </Typography>
            </Stack>
          ))}
        </>
      )}
    </Box>
  );
}
