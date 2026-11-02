import { useEffect, useRef, useState } from "react";
import { Box, Chip, TextField } from "@mui/material";
import { Group, SettingRow, StatusLine } from "../../components/SettingsKit";
import { characterApi } from "../../api/client";

// 角色卡：编辑 character.md（人格设定，随 system prompt 注入）；失焦即保存。
export default function CharacterSection() {
  const [text, setText] = useState("");
  const [name, setName] = useState("OmniAgent");
  const [saved, setSaved] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const dirtyRef = useRef(false);

  useEffect(() => {
    characterApi
      .get()
      .then((r) => {
        setText(r.data.character);
        setName(r.data.name);
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  const save = async () => {
    if (!dirtyRef.current) return;
    dirtyRef.current = false;
    setSaved("");
    setError("");
    try {
      const r = await characterApi.update(text);
      setName(r.data?.name ?? "OmniAgent");
      setSaved("已保存角色卡，下一次对话生效");
    } catch (e) {
      dirtyRef.current = true;
      setError((e as Error).message);
    }
  };

  return (
    <Group title="角色卡" desc="助手的人格与相处方式，作为 system prompt 的一部分注入。">
      <SettingRow
        title="助手名"
        desc="可在文件顶部用 frontmatter 指定（--- name: 小助 ---）；不写则默认 OmniAgent。"
      >
        <Chip size="small" label={name} variant="outlined" />
      </SettingRow>
      <Box sx={{ pt: 2 }}>
        <TextField
          fullWidth
          multiline
          minRows={14}
          maxRows={24}
          placeholder={"---\nname: 小助\n---\n\n# 角色\n你是我的伙伴……\n\n## 语气\n- 简洁、直接\n"}
          value={text}
          onChange={(e) => { dirtyRef.current = true; setText(e.target.value); }}
          onBlur={() => void save()}
          disabled={loading}
          sx={{
            "& .MuiOutlinedInput-notchedOutline": { borderColor: "divider" },
            "& textarea": { fontFamily: "monospace", fontSize: 13, lineHeight: 1.7 },
          }}
        />
        <StatusLine saved={saved} error={error} />
      </Box>
    </Group>
  );
}
