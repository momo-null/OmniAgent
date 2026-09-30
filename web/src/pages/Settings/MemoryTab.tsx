import { useEffect, useRef, useState } from "react";
import {
  Box, TextField, Button, Alert, Typography, Chip, Stack,
  List, ListItemButton, ListItemText, Divider, Drawer, IconButton,
  Dialog, DialogTitle, DialogContent, DialogContentText, DialogActions, Paper,
  useTheme,
} from "@mui/material";
import DeleteIcon from "@mui/icons-material/Delete";
import RestartAltIcon from "@mui/icons-material/RestartAlt";
import AutoStoriesIcon from "@mui/icons-material/AutoStories";
import HistoryEduIcon from "@mui/icons-material/HistoryEdu";
import LockIcon from "@mui/icons-material/Lock";
import { memoryApi } from "../../api/client";
import type { MemoryIndex, RolloutInfo, RolloutDetail } from "../../types";

// 长期记忆（伙伴 → 记忆）：编辑 MEMORY.md + 统计 + rollouts 溯源/删除 + 一键重置。
// 注入开关在「通用」页，本页只读展示状态。此为记忆的唯一入口（技能和工具页的入口已移除）。
export default function MemoryTab() {
  const theme = useTheme();
  const [master, setMaster] = useState("");
  const [idx, setIdx] = useState<MemoryIndex | null>(null);
  const [rollouts, setRollouts] = useState<RolloutInfo[]>([]);
  const [saved, setSaved] = useState("");
  const [error, setError] = useState("");
  const [drawer, setDrawer] = useState<RolloutDetail | null>(null);
  const [resetOpen, setResetOpen] = useState(false);
  const [resetText, setResetText] = useState("");

  const refresh = () =>
    Promise.all([memoryApi.index(), memoryApi.rollouts({ limit: 100 })])
      .then(([i, ro]) => {
        setIdx(i.data);
        setMaster(i.data.master);
        setRollouts((ro.data as { rollouts: RolloutInfo[] }).rollouts || []);
      })
      .catch((e) => setError(e.message));

  useEffect(() => {
    refresh();
  }, []);

  const dirtyRef = useRef(false);
  const save = async () => {
    if (!dirtyRef.current) return;
    dirtyRef.current = false;
    setSaved("");
    setError("");
    try {
      await memoryApi.update(master);
      setSaved("已保存长期记忆，并已重生成注入视图");
      refresh();
    } catch (e) {
      dirtyRef.current = true;
      setError((e as Error).message);
    }
  };

  const handleDeleteRollout = async (taskId: string) => {
    try {
      await memoryApi.deleteRollout(taskId);
      setDrawer(null);
      refresh();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const handleReset = async () => {
    if (resetText.trim() !== "reset") return;
    try {
      await memoryApi.reset();
      setResetOpen(false);
      setResetText("");
      refresh();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <Box>
      <Alert severity="info" sx={{ mb: 2 }}>
        长期记忆回答「过去发生了什么」（跨任务的事实与经验）。需在「通用」里开启
        “注入全局记忆摘要” 才会注入。
      </Alert>
      {saved && (
        <Alert severity="success" sx={{ mb: 2 }} onClose={() => setSaved("")}>{saved}</Alert>
      )}
      {error && (
        <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError("")}>{error}</Alert>
      )}

      {/* 统计条 + 重置 */}
      <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap sx={{ mb: 2 }}>
        <Chip size="small" icon={<AutoStoriesIcon />} label={`rollouts ${idx?.rollouts_total ?? 0}`} variant="outlined" />
        <Chip size="small" icon={<HistoryEduIcon />} label={`已合并 ${idx?.merged_total ?? 0}`} variant="outlined" />
        <Chip size="small" icon={<LockIcon />} label={`注入视图 ${idx?.summary_chars ?? 0} 字`} variant="outlined" />
        <Chip
          size="small"
          label={idx?.enabled ? "注入已开" : "注入已关"}
          color={idx?.enabled ? "success" : "default"}
          variant="outlined"
        />
        <Box sx={{ flexGrow: 1 }} />
        <Button size="small" color="error" startIcon={<RestartAltIcon />} onClick={() => setResetOpen(true)}>
          重置记忆
        </Button>
      </Stack>

      {/* ① MEMORY.md 编辑器 */}
      <Typography variant="subtitle2" gutterBottom>记忆正文（MEMORY.md）</Typography>
      <Paper
        elevation={0}
        sx={{
          p: 1.5, borderRadius: 1, bgcolor: "background.paper",
          border: "1px solid", borderColor: "divider",
          maxHeight: 180, overflow: "auto", whiteSpace: "pre-wrap", wordBreak: "break-word",
          fontSize: 13, minHeight: 60, mb: 1,
        }}
      >
        {master ? master : "（MEMORY.md 为空，蒸馏产物合并或你手动编辑后会出现内容）"}
      </Paper>
      <TextField
        fullWidth
        multiline
        minRows={12}
        maxRows={22}
        placeholder={"# Long-term Memory\n\n## 事实\n- \n\n## 经验\n- \n"}
        value={master}
        onChange={(e) => { dirtyRef.current = true; setMaster(e.target.value); }}
        onBlur={() => void save()}
        helperText="失焦即保存"
        sx={{ "& textarea": { fontFamily: "monospace", fontSize: 13 } }}
      />

      <Divider sx={{ my: 2 }} />

      {/* ② rollouts 溯源列表 */}
      <Typography variant="subtitle2" gutterBottom>记忆回放溯源（rollouts）</Typography>
      {rollouts.length === 0 ? (
        <Typography variant="body2" color="text.secondary">
          暂无 rollout。任务完成后 Curator 静默蒸馏（成功任务抽 facts，失败任务记 lessons），每满 5 条未合并项合并进 MEMORY.md。
        </Typography>
      ) : (
        <List dense>
          {rollouts.map((r) => (
            <ListItemButton key={r.task_id} onClick={() => memoryApi.rollout(r.task_id).then((res) => setDrawer(res.data)).catch(() => setError("读取 rollout 失败"))}>
              <ListItemText
                primary={
                  <Stack direction="row" spacing={1} alignItems="center">
                    <span style={{ fontFamily: "monospace", fontSize: 13 }}>{r.task_id}</span>
                    {r.success === true && <Chip size="small" label="成功" color="success" />}
                    {r.success === false && <Chip size="small" label="失败" color="warning" />}
                    {r.merged && <Chip size="small" label="已合并" variant="outlined" />}
                  </Stack>
                }
                secondary={`facts:${r.facts_n} · lessons:${r.lessons_n}${r.distilled_at ? " · " + r.distilled_at : ""}`}
              />
            </ListItemButton>
          ))}
        </List>
      )}

      {/* ③ 单条 rollout 全文抽屉 */}
      <Drawer anchor="right" open={Boolean(drawer)} onClose={() => setDrawer(null)} sx={{ zIndex: theme.zIndex.drawer + 1 }}>
        <Box sx={{ width: 460, p: 2, display: "flex", flexDirection: "column", minHeight: 0 }}>
          <Stack direction="row" justifyContent="space-between" alignItems="center" sx={{ mb: 1 }}>
            <Typography variant="subtitle2" noWrap>
              rollout: {drawer?.task_id}
            </Typography>
            <IconButton size="small" onClick={() => setDrawer(null)}><DeleteIcon fontSize="small" /></IconButton>
          </Stack>
          {drawer && (
            <>
              <Typography variant="caption" color="text.secondary" sx={{ mb: 1 }}>
                trajectory: {drawer.trajectory || "（无）"} · facts:{drawer.facts_n} · lessons:{drawer.lessons_n}
              </Typography>
              <Box sx={{ flexGrow: 1, minHeight: 0, overflow: "auto" }}>
                <Box component="pre" sx={{ whiteSpace: "pre-wrap", wordBreak: "break-word", fontSize: 12, m: 0 }}>
                  {drawer.content}
                </Box>
              </Box>
              <Button
                size="small" color="error" sx={{ mt: 1 }} startIcon={<DeleteIcon />}
                onClick={() => drawer && handleDeleteRollout(drawer.task_id)}
              >
                删除此 rollout（不回滚已合并内容）
              </Button>
            </>
          )}
        </Box>
      </Drawer>

      {/* ④ 重置确认对话框 */}
      <Dialog open={resetOpen} onClose={() => setResetOpen(false)} maxWidth="xs" fullWidth>
        <DialogTitle>重置全部记忆</DialogTitle>
        <DialogContent>
          <DialogContentText>
            将清空 memory/ 全部产物（MEMORY.md / 注入视图 / 全部 rollouts / 合并清单）。
            此操作不可恢复，且不触及任何任务私有数据。输入 <b>reset</b> 确认。
          </DialogContentText>
          <TextField
            autoFocus fullWidth margin="dense" label="输入 reset"
            value={resetText} onChange={(e) => setResetText(e.target.value)}
          />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => { setResetOpen(false); setResetText(""); }}>取消</Button>
          <Button color="error" disabled={resetText.trim() !== "reset"} onClick={handleReset}>
            确认重置
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
