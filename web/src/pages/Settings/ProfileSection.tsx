import { useEffect, useRef, useState } from "react";
import { Box, TextField } from "@mui/material";
import { Group, Hint, StatusLine } from "../../components/SettingsKit";
import { profileApi } from "../../api/client";

// 她的自我画像：编辑 self_profile.md 正文；失焦即保存。
// （知识分层 B1：候选区死平面已删除——它曾为「人工确认」而设计，现无生产者；
//   画像由用户直接维护，LLM 自动维护（巩固周期 L3 增量合并）已接入。）
export default function ProfileSection() {
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
      setSaved("已保存自我画像，下一轮对话注入");
    } catch (e) {
      dirtyRef.current = true;
      setError((e as Error).message);
    }
  };

  return (
    <Group title="她的自我画像" desc="回答「她自己是谁」——她的兴趣、品味、生活史与能力面（角色本人的演化层，静态底色在角色卡）；作为独立块注入（仅参考、无放行权）。">
      <Box>
        <TextField
          fullWidth
          multiline
          minRows={12}
          maxRows={24}
          placeholder={"# User Profile\n\n## 基本信息\n- 称呼：\n\n## 偏好\n- \n"}
          value={profile}
          onChange={(e) => { dirtyRef.current = true; setProfile(e.target.value); }}
          onBlur={() => void save()}
          sx={{
            "& .MuiOutlinedInput-notchedOutline": { borderColor: "divider" },
            "& textarea": { fontFamily: "monospace", fontSize: 13, lineHeight: 1.7 },
          }}
        />
        <Hint>
          失焦即保存。{enabled
            ? "自我画像注入当前已开启。"
            : "自我画像注入当前已关闭（设置 knowledge.profile.enabled）。"}
        </Hint>
        <StatusLine saved={saved} error={error} />
      </Box>
    </Group>
  );
}
