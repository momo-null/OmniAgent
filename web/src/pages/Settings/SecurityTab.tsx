// S0/S2 设置页「安全」区：权限模式档位 / 允许写入根 / 审批等待 / 审计查看。
// 语义：
// - 档位随 run 快照生效——改动不影响进行中的 run，下次 run 生效；
// - 允许根只决定「写」的放行区（~/.omniagent 自身永远拒绝，任何档位都不例外）；
// - 审计只读（门的干预才记录；agent 不可触达）。
import { useEffect, useState } from "react";
import {
  Alert,
  Box,
  Button,
  FormControl,
  FormControlLabel,
  Paper,
  Radio,
  RadioGroup,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  TextField,
  Typography,
  Chip,
} from "@mui/material";
import { settingsApi, approvalsApi } from "../../api/client";

const DECISION_COLOR: Record<string, "success" | "error" | "warning" | "default"> = {
  approved: "success",
  user_deny: "error",
  timeout: "warning",
  cancelled: "default",
  denied_s1: "error",
  mode_read_only: "warning",
  auto_deny: "warning",
};

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

export default function SecurityTab() {
  const [mode, setMode] = useState<string>("standard");
  const [roots, setRoots] = useState<string[]>([]);
  const [newRoot, setNewRoot] = useState("");
  const [waitSeconds, setWaitSeconds] = useState<number>(600);
  const [auditOn, setAuditOn] = useState<boolean>(true);
  const [entries, setEntries] = useState<AuditEntry[]>([]);
  const [saved, setSaved] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    settingsApi.get().then((r) => {
      const sec = (r.data as any)?.security || {};
      if (typeof sec.mode === "string") setMode(sec.mode);
      if (Array.isArray(sec.allow_write_roots)) setRoots(sec.allow_write_roots.map(String));
      const w = (sec.approval || {}).wait_seconds;
      if (typeof w === "number") setWaitSeconds(w);
      if (typeof sec.audit === "boolean") setAuditOn(sec.audit);
    }).catch(() => {});
    refreshAudit();
  }, []);

  const refreshAudit = () => {
    approvalsApi.audit(200).then((r) => {
      setEntries(((r.data as any)?.items || []) as AuditEntry[]);
    }).catch(() => {});
  };

  const addRoot = () => {
    const v = newRoot.trim();
    if (!v || roots.includes(v)) return;
    setRoots([...roots, v]);
    setNewRoot("");
  };

  const handleSave = async () => {
    setError(""); setSaved("");
    try {
      await settingsApi.put({
        security: {
          mode,
          allow_write_roots: roots,
          approval: { wait_seconds: waitSeconds >= 0 ? waitSeconds : 600 },
          audit: auditOn,
        },
      });
      setSaved("已保存到 ~/.omniagent/config.yaml（变更自下次运行生效）");
    } catch (e) { setError((e as Error).message); }
  };

  return (
    <Box>
      <Typography variant="subtitle2" gutterBottom>安全与审批</Typography>
      <Stack spacing={2} sx={{ maxWidth: 560, mb: 3 }}>
        <Alert severity="info">
          「标准」档：危险动作（命令执行 / 键鼠 / 允许根外写入）逐项弹卡人工批准；
          「只读」档：危险动作自动拒绝（挂机实验 / 不可信内容用）。
          每个任务的「完全访问」开关在 Chat 页单独控制，开启后不弹卡——
          各任务独立记录在 task.json，跟随当前任务切换。
        </Alert>
        <FormControl>
          <RadioGroup row value={mode} onChange={(e) => setMode(e.target.value)}>
            <FormControlLabel value="standard" control={<Radio />} label="标准（弹卡审批）" />
            <FormControlLabel value="read_only" control={<Radio />} label="只读（自动拒绝）" />
          </RadioGroup>
        </FormControl>
        <TextField
          label="审批等待上限（秒，0 = 无限等，超时按拒绝处理）" type="number"
          value={waitSeconds} inputProps={{ min: 0 }}
          onChange={(e) => setWaitSeconds(Math.max(0, Number(e.target.value) || 0))} />
        <Box>
          <Typography variant="body2" gutterBottom>允许写入的根（任务临时目录之外；路径围栏的写入放行区）</Typography>
          <Stack direction="row" spacing={1}>
            <TextField size="small" fullWidth placeholder="绝对路径，如 D:\\projects"
              value={newRoot} onChange={(e) => setNewRoot(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") addRoot(); }} />
            <Button variant="outlined" onClick={addRoot}>添加</Button>
          </Stack>
          <Stack direction="row" spacing={1} sx={{ mt: 1, flexWrap: "wrap", gap: 0.5 }}>
            {roots.map((r) => (
              <Chip key={r} label={r} size="small" onDelete={() => setRoots(roots.filter((x) => x !== r))} />
            ))}
            {roots.length === 0 && (
              <Typography variant="caption" color="text.secondary">
                （未配置——写入仅限当前任务目录，其余位置逐项审批）
              </Typography>
            )}
          </Stack>
        </Box>
        <Stack direction="row" justifyContent="space-between" alignItems="center">
          <Button variant="contained" onClick={handleSave}>保存</Button>
          <Typography variant="caption" color="text.secondary">审计：{auditOn ? "开启" : "关闭"}</Typography>
        </Stack>
        {saved && <Alert severity="success">{saved}</Alert>}
        {error && <Alert severity="error">{error}</Alert>}
      </Stack>

      <Stack direction="row" justifyContent="space-between" alignItems="center" sx={{ mb: 1 }}>
        <Typography variant="subtitle2">审计记录（尾部 {entries.length} 条）</Typography>
        <Button size="small" onClick={refreshAudit}>刷新</Button>
      </Stack>
      <TableContainer component={Paper} variant="outlined" sx={{ maxHeight: 360 }}>
        <Table size="small" stickyHeader>
          <TableHead>
            <TableRow>
              <TableCell>时间</TableCell>
              <TableCell>决策</TableCell>
              <TableCell>规则</TableCell>
              <TableCell>工具</TableCell>
              <TableCell>任务</TableCell>
              <TableCell>参数</TableCell>
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
    </Box>
  );
}
