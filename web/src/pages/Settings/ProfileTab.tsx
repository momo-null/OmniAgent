import { useEffect, useRef, useState } from "react";
import { Box, TextField, Alert, Typography } from "@mui/material";
import { profileApi } from "../../api/client";

// 用户画像设置：编辑 user_profile.md 正文；失焦即保存
// （知识分层 B1：候选区死平面已删除——它曾为「人工确认」而设计，现无生产者；
//   画像由用户直接维护，LLM 自动维护待 A8 批量归纳基建落地后接入。）
export default function ProfileTab() {
  const [profile, setProfile] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [saved, setSaved] = useState("");
  const [error, setError] = useState("");
  const dirtyRef = useRef(false);

  const refresh = () =>
    profileApi
      .get()
      .then((r) => {
        setProfile(r.data.profile);
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
        用户画像回答「用户是谁」，作为独立块注入（仅参考、无放行权）。
        {enabled ? "注入当前已开启。" : "注入当前已关闭（设置 knowledge.profile.enabled）。"}
      </Alert>
      <Typography variant="subtitle2" gutterBottom>
        画像正文（user_profile.md）
      </Typography>
      <TextField
        fullWidth
        multiline
        minRows={12}
        maxRows={24}
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
    </Box>
  );
}
