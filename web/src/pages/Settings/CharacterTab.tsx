import { useEffect, useState } from "react";
import { Box, Stack, TextField, Button, Alert, Typography, Chip } from "@mui/material";
import { characterApi } from "../../api/client";

// 角色卡设置：编辑 character.md（人格设定，随 system prompt 注入）
export default function CharacterTab() {
  const [text, setText] = useState("");
  const [name, setName] = useState("OmniAgent");
  const [saved, setSaved] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

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

  const handleSave = async () => {
    setSaved("");
    setError("");
    try {
      const r = await characterApi.update(text);
      setName(r.data?.name ?? "OmniAgent");
      setSaved("已保存角色卡，下一次对话生效（随 system prompt 注入）");
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <Box>
      <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }}>
        <Typography variant="subtitle2">助手名：</Typography>
        <Chip size="small" label={name} color="primary" variant="outlined" />
      </Stack>
      <Alert severity="info" sx={{ mb: 2 }}>
        角色卡定义助手的人格与相处方式，随 system prompt 注入。可在文件顶部用 frontmatter
        指定名字：<code>--- name: 小助 ---</code>；不写则默认 “OmniAgent”。
      </Alert>
      <TextField
        fullWidth
        multiline
        minRows={14}
        maxRows={24}
        placeholder={"---\nname: 小助\n---\n\n# 角色\n你是我的伙伴……\n\n## 语气\n- 简洁、直接\n"}
        value={text}
        onChange={(e) => setText(e.target.value)}
        sx={{ "& textarea": { fontFamily: "monospace", fontSize: 13 } }}
      />
      <Button variant="contained" onClick={handleSave} sx={{ mt: 1 }} disabled={loading}>
        保存角色卡
      </Button>
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
