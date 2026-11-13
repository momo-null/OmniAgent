import { useEffect, useState } from "react";
import { Box, Button, Chip, IconButton, InputAdornment, Stack, TextField, Tooltip } from "@mui/material";
import { AddLink, LinkOff, Search } from "@mui/icons-material";
import { Group, Hint, StatusLine } from "../../components/SettingsKit";
import { identityApi } from "../../api/client";

// 主人身份（认领）：Web 控制台恒为主人（零配置）；通道侧（QQ/飞书/…）的标识
// 在这里「认领」——点「这是我」即把该说话人并入主人卡（回填历史 + 收编旧卡 +
// 宽窗重建）。解除认领同界面。设计见 doc/plans/implemented/memory-architecture.md §2.4。
export default function MasterIdentitySection() {
  const [uids, setUids] = useState<string[]>([]);
  const [cardExists, setCardExists] = useState(false);
  const [cardUpdated, setCardUpdated] = useState("");
  const [speakers, setSpeakers] = useState<
    { name: string; uid: string; lines: number; last: string }[]
  >([]);
  const [q, setQ] = useState("");
  const [saved, setSaved] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const refresh = () =>
    Promise.all([identityApi.master(), identityApi.speakers("")])
      .then(([m, s]) => {
        setUids(m.data.uids || []);
        setCardExists(!!m.data.card?.exists);
        setCardUpdated(m.data.card?.updated || "");
        setSpeakers(s.data.items || []);
      })
      .catch((e) => setError(e.message));

  useEffect(() => {
    refresh();
  }, []);

  const search = (value: string) => {
    setQ(value);
    identityApi
      .speakers(value.trim())
      .then((r) => setSpeakers(r.data.items || []))
      .catch((e) => setError(e.message));
  };

  const claim = async (uid: string, name: string) => {
    setBusy(true);
    setSaved("");
    setError("");
    try {
      const r = await identityApi.claim(uid);
      const d = r.data;
      setSaved(`已认领 ${name}：回填 ${d.backfilled} 条、重建主人卡${d.card_rebuilt ? "完成" : "（材料不足待积累）"}`);
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const unclaim = async (uid: string) => {
    setBusy(true);
    setSaved("");
    setError("");
    try {
      await identityApi.unclaim(uid);
      setSaved(`已解除 ${uid}（之后该标识的新消息按普通说话人重新分组）`);
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Group
      title="主人身份"
      desc="Web 控制台恒为主人（admin，零配置）。在通道上（QQ / 飞书 / …）跟她说过话的标识，在这里点「这是我」认领——历史会并入主人卡。"
    >
      <StatusLine saved={saved} error={error} />
      <Stack spacing={1}>
        <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap">
          <Chip size="small" label="Web = 主人（恒生效）" color="success" variant="outlined" />
          <Chip
            size="small"
            label={cardExists ? `主人卡${cardUpdated ? ` · ${cardUpdated.slice(0, 10)}` : ""}` : "主人卡待积累"}
            variant="outlined"
          />
        </Stack>
        {uids.length > 0 && (
          <Box>
            {(uids || []).map((u) => (
              <Stack key={u} direction="row" spacing={1} alignItems="center" sx={{ py: 0.25 }}>
                <Box sx={{ fontFamily: "monospace", fontSize: 12 }}>{u}</Box>
                <Tooltip title="解除认领：之后该标识的新消息按普通说话人重新分组">
                  <IconButton size="small" disabled={busy} onClick={() => void unclaim(u)}>
                    <LinkOff fontSize="small" />
                  </IconButton>
                </Tooltip>
              </Stack>
            ))}
          </Box>
        )}
        <TextField
          size="small"
          fullWidth
          placeholder="搜索说话人（昵称 / 标识前缀，不限最近 7 天）"
          value={q}
          onChange={(e) => search(e.target.value)}
          InputProps={{
            startAdornment: (
              <InputAdornment position="start">
                <Search fontSize="small" />
              </InputAdornment>
            ),
            style: { fontSize: 12 },
          }}
        />
        {speakers.length === 0 ? (
          <Hint>近 7 天没有未认领的活跃说话人（≥3 条才入列）。</Hint>
        ) : (
          speakers.map((s) => (
            <Stack
              key={s.uid}
              direction="row"
              spacing={1}
              alignItems="center"
              justifyContent="space-between"
              sx={{
                borderBottom: "1px solid",
                borderColor: "divider",
                py: 0.4,
              }}
            >
              <Box sx={{ minWidth: 0, flex: 1 }}>
                <Box sx={{ fontSize: 12, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                  {s.name}
                  <Box component="span" sx={{ fontFamily: "monospace", color: "text.secondary", ml: 0.75 }}>
                    {s.uid}
                  </Box>
                </Box>
              </Box>
              <Box sx={{ fontSize: 11, color: "text.secondary", whiteSpace: "nowrap" }}>
                近段 {s.lines} 条
              </Box>
              <Tooltip title="这是我：并入主人卡（回填历史 + 收编旧卡 + 重建）">
                <span>
                  <Button
                    size="small"
                    startIcon={<AddLink />}
                    disabled={busy || uids.includes(s.uid)}
                    onClick={() => void claim(s.uid, s.name)}
                    sx={{ minWidth: 0 }}
                  >
                    这是我
                  </Button>
                </span>
              </Tooltip>
            </Stack>
          ))
        )}
        <Hint>认领按标识匹配，不区分通道；多个号可逐个认领。一次性聊过的人不会出现在这里（关系卡满 3 条往来才生成）。</Hint>
      </Stack>
    </Group>
  );
}
