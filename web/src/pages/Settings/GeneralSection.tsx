import { useEffect, useRef, useState } from "react";
import { Box, Typography } from "@mui/material";
import { Group, Hint, NumberRow, StatusLine, SwitchRow } from "../../components/SettingsKit";
import { settingsApi } from "../../api/client";
import { useTaskStore } from "../../store/taskStore.tsx";

// 通用：任务总预算与技能层开关。全部「改动即写回 ~/.omniagent/config.yaml」，
// 数字失焦提交、开关点击即时提交，没有整页保存按钮。
export default function GeneralSection() {
  const { setMaxSteps } = useTaskStore();
  const [draft, setDraft] = useState<number>(40);
  const [autoDistill, setAutoDistill] = useState<boolean>(false);
  const [saved, setSaved] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    settingsApi.get().then((r) => {
      const d = r.data as Record<string, any>;
      const v = d?.runtime?.default_max_steps;
      if (typeof v === "number" && v > 0) { setDraft(v); setMaxSteps(v); }
      const ad = d?.skill?.auto_distill;
      if (typeof ad === "boolean") setAutoDistill(ad);
    }).catch(() => {});
  }, [setMaxSteps]);

  const dirtyRef = useRef(false);
  const save = async (adOverride?: boolean) => {
    if (!dirtyRef.current) return;
    dirtyRef.current = false;
    setError(""); setSaved("");
    try {
      const v = draft > 0 ? Math.min(draft, 2000) : 40;
      const ad = adOverride ?? autoDistill;
      await settingsApi.put({
        runtime: { default_max_steps: v },
        skill: { auto_distill: ad },
      });
      setMaxSteps(v);
      setSaved("已保存，下一次运行生效");
    } catch (e) { dirtyRef.current = true; setError((e as Error).message); }
  };

  return (
    <Box>
      <Group title="运行预算">
        <NumberRow
          title="默认步数上限"
          desc="单次任务的工具/决策总步数预算，耗尽即停止（budget_exhausted）。新会话默认采用此值；后端未显式传 max_steps 时也回退到此值。范围 1–2000。"
          value={draft} min={1} max={2000}
          onChange={(v) => { dirtyRef.current = true; setDraft(Math.min(v || 40, 2000)); }}
          onBlur={() => void save()}
        />
        <SwitchRow
          title="自动蒸馏技能"
          desc="开启后，任务成功时由模型自动提炼技能（LLM 归纳引导型 playbook，含防幻觉校验，观察不到可复用规律就交白卷）。默认关闭：该能力在另一场景上未取得正向证据。关闭时已有技能仍可检索与加载，人工维护不受影响。"
          checked={autoDistill}
          onChange={(v) => { dirtyRef.current = true; setAutoDistill(v); void save(v); }}
        />
        <StatusLine saved={saved} error={error} />
      </Group>

      <Group title="数据位置">
        <Typography variant="body2" color="text.secondary" sx={{ lineHeight: 1.7 }}>
          数据根目录 <Box component="code" sx={{ fontFamily: "monospace", fontSize: 12 }}>~/.omniagent/</Box>（全局层）。
          任务资产按 task 平铺存放，不依赖工作目录。
        </Typography>
        <Hint>这里的改动即时写回配置文件，不需要点保存；页脚不会残留未提交的内容。</Hint>
      </Group>
    </Box>
  );
}
