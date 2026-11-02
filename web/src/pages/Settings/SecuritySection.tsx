// 安全与审批：权限模式档位 / 审批等待 / 审计查看。
// 语义：
// - 档位随 run 快照生效——改动不影响进行中的 run，下次 run 生效；
// - 写入不设防（S1 路径围栏已整体删除，含允许根）；风险靠档位与危险动作审批把关；
// - 审计只读（门的干预才记录；agent 不可触达）。
import { useEffect, useRef, useState } from "react";
import {
  Box,
  Button,
  Chip,
  Radio,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Typography,
  Paper,
} from "@mui/material";
import { Group, Hint, NumberRow, SettingRow, StatusLine } from "../../components/SettingsKit";
import { settingsApi, approvalsApi } from "../../api/client";

const DECISION_COLOR: Record<string, "success" | "error" | "warning" | "default"> = {
  approved: "success",
  user_deny: "error",
  timeout: "warning",
  cancelled: "default",
  mode_read_only: "warning",
  auto_deny: "warning",
};

const MODES = [
  { value: "standard", label: "标准", desc: "危险动作（命令执行 / 键鼠）逐项弹卡人工批准，文件写入不设门。" },
  { value: "read_only", label: "只读", desc: "危险动作自动拒绝，用于挂机实验或处理不可信内容。" },
  { value: "full_access", label: "完全访问", desc: "所有任务/运行的危险动作直接放行、不弹卡（信任本机环境时用）。每个任务还可在对话页单独开启，任一为真即免审批。" },
];

interface AuditEntry {
  ts: number;
  task_id: string;
  tool: string;
  unit: string;
  risk: string;
  arguments: string;
  decision: string;
  rule: string;
  wait_ms: number;
}

export default function SecuritySection() {
  const [mode, setMode] = useState<string>("standard");
  const [waitSeconds, setWaitSeconds] = useState<number>(600);
  const [auditOn, setAuditOn] = useState<boolean>(true);
  const [entries, setEntries] = useState<AuditEntry[]>([]);
  const [saved, setSaved] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    settingsApi.get().then((r) => {
      const sec = (r.data as Record<string, any>)?.security || {};
      if (typeof sec.mode === "string") setMode(sec.mode);
      const w = (sec.approval || {}).wait_seconds;
      if (typeof w === "number") setWaitSeconds(w);
      if (typeof sec.audit === "boolean") setAuditOn(sec.audit);
    }).catch(() => {});
    refreshAudit();
  }, []);

  const refreshAudit = () => {
    approvalsApi.audit(200).then((r) => {
      setEntries(((r.data as Record<string, any>)?.items || []) as AuditEntry[]);
    }).catch(() => {});
  };

  // 失焦/勾选即保存（dirty 检查，无变化不提交）；档位切换即时生效
  const dirtyRef = useRef(false);
  const save = async (overrides: Record<string, unknown> = {}) => {
    if (!dirtyRef.current) return;
    dirtyRef.current = false;
    setError(""); setSaved("");
    const s = { mode, waitSeconds, ...overrides } as { mode: string; waitSeconds: number };
    try {
      await settingsApi.put({
        security: {
          mode: s.mode,
          approval: { wait_seconds: s.waitSeconds >= 0 ? s.waitSeconds : 600 },
          audit: auditOn,
        },
      });
      setSaved("已保存（变更自下次运行生效）");
    } catch (e) { dirtyRef.current = true; setError((e as Error).message); }
  };

  return (
    <Box>
      <Group title="权限档位" desc="决定危险动作要不要问人；改动自下次运行生效。">
        {MODES.map((m) => {
          const selected = m.value === mode;
          return (
            <Box key={m.value} sx={{ display: "flex", gap: 1.5, alignItems: "flex-start", py: 1.5,
                                     borderTop: "1px solid", borderColor: "divider" }}>
              <Radio size="small" checked={selected} sx={{ pt: 0.25 }}
                onChange={() => { const v = m.value; dirtyRef.current = true; setMode(v); void save({ mode: v }); }} />
              <Box sx={{ minWidth: 0 }}>
                <Typography variant="body2" sx={{ fontWeight: 500, color: selected ? "text.primary" : "text.secondary" }}>
                  {m.label}
                </Typography>
                <Typography variant="caption" color="text.secondary" sx={{ display: "block", lineHeight: 1.6 }}>
                  {m.desc}
                </Typography>
              </Box>
            </Box>
          );
        })}
        <NumberRow
          title="审批等待上限（秒）"
          desc="弹卡后人最多等多久；0 = 无限等，超时按拒绝处理。"
          value={waitSeconds} min={0} step={30}
          onChange={(v) => { dirtyRef.current = true; setWaitSeconds(Math.max(0, v || 0)); }}
          onBlur={() => void save()}
        />
        <SettingRow title="审计记录" desc="只记录「门」的干预（人工批准/拒绝/超时），agent 不可触达。">
          <Typography variant="body2" color="text.secondary">
            {auditOn ? "开启" : "关闭"}
          </Typography>
        </SettingRow>
        <StatusLine saved={saved} error={error} />
        <Hint>
          每个任务的「完全访问」开关在对话页输入框旁单独控制，记录在 task.json 并跟随当前任务切换；
          这里改的是全局档。
        </Hint>
      </Group>

      <Group title={`审计记录（尾部 ${entries.length} 条）`}>
        <Stack direction="row" justifyContent="flex-end" sx={{ mb: 1 }}>
          <Button size="small" onClick={refreshAudit}>刷新</Button>
        </Stack>
        <TableContainer component={Paper} variant="outlined" sx={{ maxHeight: 360, bgcolor: "transparent" }}>
          <Table size="small" stickyHeader>
            <TableHead>
              <TableRow>
                {["时间", "决策", "规则", "工具", "任务", "参数"].map((h) => (
                  <TableCell key={h}>{h}</TableCell>
                ))}
              </TableRow>
            </TableHead>
            <TableBody>
              {entries.length === 0 && (
                <TableRow><TableCell colSpan={6}>
                  <Typography variant="caption" color="text.secondary">（暂无记录——门的干预才会记录）</Typography>
                </TableCell></TableRow>
              )}
              {entries.map((e, i) => (
                <TableRow key={i}>
                  <TableCell sx={{ whiteSpace: "nowrap" }}>
                    {new Date(e.ts * 1000).toLocaleString()}
                  </TableCell>
                  <TableCell>
                    <Chip size="small" color={DECISION_COLOR[e.decision] ?? "default"} label={e.decision} />
                  </TableCell>
                  <TableCell>{e.rule}</TableCell>
                  <TableCell>{e.tool}{e.risk ? ` (${e.risk})` : ""}</TableCell>
                  <TableCell sx={{ maxWidth: 120, overflow: "hidden", textOverflow: "ellipsis" }}>{e.task_id}</TableCell>
                  <TableCell sx={{ maxWidth: 260, overflow: "hidden", textOverflow: "ellipsis",
                    fontFamily: "monospace", fontSize: 11 }} title={e.arguments}>{e.arguments}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableContainer>
      </Group>
    </Box>
  );
}
