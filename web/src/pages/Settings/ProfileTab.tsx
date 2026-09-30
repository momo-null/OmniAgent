import { useEffect, useRef, useState } from "react";
import { Box, TextField, Alert, Typography, Divider } from "@mui/material";
import { profileApi } from "../../api/client";

// 用户画像设置：编辑 user_profile.md 正文 + 查看自动蒸馏的候选区；失焦即保存
export default function ProfileTab() {
  const [profile, setProfile] = useState("");
  const [candidates, setCandidates] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [saved, setSaved] = useState("");
  const [error, setError] = useState("");
  const dirtyRef = useRef(false);

  const refresh = () =>
    profileApi
      .get()
      .then((r) => {
        setProfile(r.data.profile);
        setCandidates(r.data.candidates);
        setEnabled(r.data.enabled);
      })
      .catch((e) => setError(e.message));

  useEffect(() => {
    refresh();
  }, []);

  const save = async () => {
    if (!dirtyRef.current) return;
    dirtyRef.current = false;
    setSaved("");
    setError("");
    try {
      await profileApi.update(profile);
      setSaved("已保存用户画像，下一轮对话注入");
    } catch (e) {
      dirtyRef.current = true;
      setError((e as Error).message);
    }
  };

  return (
    <Box>
      <Alert severity="info" sx={{ mb: 2 }}>
        用户画像回答「用户是谁」，作为独立块注入（仅参考、无放行权）。下方候选区是从你的
        纠偏自动蒸馏的待确认条目；确认是长期事实后，把它整理进画像正文并保存。
      </Alert>
      <Typography variant="subtitle2" gutterBottom>
        画像正文（user_profile.md）
      </Typography>
      <TextField
        fullWidth
        multiline
        minRows={10}
        maxRows={20}
        placeholder={"# User Profile\n\n## 基本信息\n- 称呼：\n\n## 偏好\n- \n"}
        value={profile}
        onChange={(e) => { dirtyRef.current = true; setProfile(e.target.value); }}
        onBlur={() => void save()}
        helperText="失焦即保存"
        sx={{ "& textarea": { fontFamily: "monospace", fontSize: 13 } }}
      />
      {saved && (
        <Alert severity="success" sx={{ mt: 1 }}>
          {saved}
        </Alert>
      )}
      {error && (
        <Alert severity="error" sx={{ mt: 1 }}>
          {error}
        </Alert>
      )}

      <Divider sx={{ my: 2 }} />
      <Typography variant="subtitle2" gutterBottom>
        候选区（自动蒸馏，待确认 · {enabled ? "注入已开" : "注入已关"}）
      </Typography>
      <TextField
        fullWidth
        multiline
        minRows={6}
        maxRows={16}
        value={candidates}
        InputProps={{ readOnly: true }}
        placeholder={"（暂无候选。当你纠正助手且开启纠偏采集后，会出现在这里）"}
        sx={{ "& textarea": { fontFamily: "monospace", fontSize: 12 } }}
      />
      <Typography variant="caption" color="text.secondary">
        候选区为只读蒸馏产物；确认是长期事实后，请把条目整理进上方画像正文再保存。
      </Typography>
    </Box>
  );
}
