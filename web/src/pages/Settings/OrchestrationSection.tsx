import { useEffect, useRef, useState } from "react";
import { Box } from "@mui/material";
import { Group, NumberRow, StatusLine, SwitchRow } from "../../components/SettingsKit";
import { settingsApi } from "../../api/client";

// 上下文治理缺省值镜像（与后端 config.CONTEXT_DEFAULTS 保持一致）：仅作首屏占位，
// 接口返回后一律以后端「生效值」为准——缺省由代码决定，不在前端生成推荐值 UI。
const CTX_RECOMMENDED = {
  maxInputTokens: 300000, // 输入预算（Token）；当下主流大模型多为 1M 上下文，取 300k 起步
  retainRatio: 0.5,
  pruneThreshold: 8192,
  pruneHead: 4096,
  pruneTail: 1024,
  repeatGuard: 3,
  noProgressGuard: 3,
};

// 编排 / 长任务：派发节流 + 上下文治理。数字失焦即写回，开关点击即写回。
export default function OrchestrationSection() {
  const [maxRounds, setMaxRounds] = useState<number>(3);
  const [maxParallel, setMaxParallel] = useState<number>(4);
  const [budgetRatio, setBudgetRatio] = useState<number>(0.75);
  const [chunkTurns, setChunkTurns] = useState<number>(50);
  const [ltCompress, setLtCompress] = useState<boolean>(true);
  const [pruneThreshold, setPruneThreshold] = useState<number>(CTX_RECOMMENDED.pruneThreshold);
  const [pruneHead, setPruneHead] = useState<number>(CTX_RECOMMENDED.pruneHead);
  const [pruneTail, setPruneTail] = useState<number>(CTX_RECOMMENDED.pruneTail);
  const [maxInputTokens, setMaxInputTokens] = useState<number>(CTX_RECOMMENDED.maxInputTokens);
  const [retainRatio, setRetainRatio] = useState<number>(CTX_RECOMMENDED.retainRatio);
  const [compressThreshold, setCompressThreshold] = useState<number>(0);
  const [repeatGuard, setRepeatGuard] = useState<number>(CTX_RECOMMENDED.repeatGuard);
  const [noProgressGuard, setNoProgressGuard] = useState<number>(CTX_RECOMMENDED.noProgressGuard);
  const [saved, setSaved] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    settingsApi.get().then((r) => {
      const d = r.data as Record<string, any>;
      const rt = d?.runtime || {};
      const disp = rt.dispatch || {};
      const lt = rt.long_task || {};
      if (typeof disp.max_rounds === "number") setMaxRounds(disp.max_rounds);
      if (typeof disp.max_parallel === "number") setMaxParallel(disp.max_parallel);
      if (typeof lt.budget_hint_ratio === "number") setBudgetRatio(lt.budget_hint_ratio);
      if (typeof rt.chunk_turns === "number") setChunkTurns(rt.chunk_turns);
      const blt = d?.brain?.long_task || {};
      if (typeof blt.compress === "boolean") setLtCompress(blt.compress);
      // 上下文治理：后端返回的是「代码缺省 + 项目配置 + 用户配置」的生效值，直接展示
      const pr = blt.prune || {};
      if (typeof pr.threshold_chars === "number") setPruneThreshold(pr.threshold_chars);
      if (typeof pr.head_chars === "number") setPruneHead(pr.head_chars);
      if (typeof pr.tail_chars === "number") setPruneTail(pr.tail_chars);
      if (typeof blt.retain_ratio === "number") setRetainRatio(blt.retain_ratio);
      if (typeof blt.compress_threshold === "number") setCompressThreshold(blt.compress_threshold);
      if (typeof lt.repeat_guard === "number") setRepeatGuard(lt.repeat_guard);
      if (typeof lt.no_progress_guard === "number") setNoProgressGuard(lt.no_progress_guard);
      // 旧通道：输入预算挂在 brain.maxInputTokens（新 schema 在 provider 上配）
      const mit = d?.brain?.maxInputTokens ?? d?.brain?.max_input_tokens;
      if (typeof mit === "number") setMaxInputTokens(mit);
    }).catch(() => {});
  }, []);

  const dirtyRef = useRef(false);
  const saveAll = async (overrides: Record<string, unknown> = {}) => {
    if (!dirtyRef.current) return;
    dirtyRef.current = false;
    setError(""); setSaved("");
    const s = { ltCompress, ...overrides };
    try {
      await settingsApi.put({
        runtime: {
          dispatch: { max_rounds: maxRounds || 3, max_parallel: maxParallel || 4 },
          long_task: {
            budget_hint_ratio: budgetRatio,
            repeat_guard: repeatGuard || 0,
            no_progress_guard: noProgressGuard || 0,
          },
          chunk_turns: chunkTurns || 50,
        },
        brain: {
          maxInputTokens: maxInputTokens || 0,
          long_task: {
            compress: s.ltCompress,
            prune: {
              threshold_chars: pruneThreshold || 0,
              head_chars: pruneHead || 0,
              tail_chars: pruneTail || 0,
            },
            retain_ratio: retainRatio,
            compress_threshold: compressThreshold,
          },
        },
      });
      setSaved("已保存，下一次运行生效");
    } catch (e) { dirtyRef.current = true; setError((e as Error).message); }
  };

  const num = (v: number, fallback: number) => v || fallback;

  return (
    <Box>
      <Group title="派发" desc="主 agent 向子 agent 派发任务的节流阀。">
        <NumberRow
          title="派发最大轮次"
          desc="「派发 → 回收」轮次上限，防止主 agent 无限派发子任务。"
          value={maxRounds} min={1}
          onChange={(v) => { dirtyRef.current = true; setMaxRounds(num(v, 3)); }}
          onBlur={() => void saveAll()}
        />
        <NumberRow
          title="派发最大并发"
          desc="单轮最多并发多少个子 agent。"
          value={maxParallel} min={1}
          onChange={(v) => { dirtyRef.current = true; setMaxParallel(num(v, 4)); }}
          onBlur={() => void saveAll()}
        />
        <NumberRow
          title="预算提示触发比例"
          desc="任务预算用到该比例时，给 agent 一条「已用 X/Y 步」陈述性提示（仅陈述事实，不催促）。0 = 关闭。"
          value={budgetRatio} min={0} max={1} step={0.05}
          onChange={(v) => { dirtyRef.current = true; setBudgetRatio(num(v, 0.75)); }}
          onBlur={() => void saveAll()}
        />
        <NumberRow
          title="分块步数"
          desc="SDK 单次 Runner.run 内部步数分块大小；块与块之间做终止/升级检查点。一般保持 50。"
          value={chunkTurns} min={1}
          onChange={(v) => { dirtyRef.current = true; setChunkTurns(num(v, 50)); }}
          onBlur={() => void saveAll()}
        />
      </Group>

      <Group title="长任务压缩">
        <SwitchRow
          title="调用主模型生成摘要"
          desc="开启：压缩时调用主模型生成中文摘要（更省上下文）。关闭：退化为截断兜底，不消耗额外调用。"
          checked={ltCompress}
          onChange={(v) => { dirtyRef.current = true; setLtCompress(v); void saveAll({ ltCompress: v }); }}
        />
      </Group>

      <Group title="上下文治理" desc="控制送给模型的上下文如何裁剪与压缩；数字越大越保守，0 多为「关闭该项」。">
        <NumberRow
          title="模型上下文上限（Token）"
          desc="该模型的输入 Token 上限。缺省 300000（代码内置；1M 上下文模型可填 1000000）；填 0 = 显式关闭 Model 适配层粘性压缩。"
          value={maxInputTokens} min={0} step={1000}
          onChange={(v) => { dirtyRef.current = true; setMaxInputTokens(v >= 0 ? v : 0); }}
          onBlur={() => void saveAll()}
        />
        <NumberRow
          title="粘性压缩尾部保留比例"
          desc="超阈值时，尾部会话保留「上限 × 该比例」的 Token，其余压成摘要。留空/0 = 取默认 0.5。"
          value={retainRatio} min={0} max={0.9} step={0.05}
          onChange={(v) => { dirtyRef.current = true; setRetainRatio(num(v, 0.5)); }}
          onBlur={() => void saveAll()}
        />
        <NumberRow
          title="压缩触发占比"
          desc="上下文达到「模型上下文上限 × 该占比」时触发压缩；越小越早压（更省 token，摘要更频繁）。填 0 = 取默认 0.5。"
          value={compressThreshold} min={0} max={0.9} step={0.05}
          onChange={(v) => { dirtyRef.current = true; setCompressThreshold(v >= 0 ? v : 0); }}
          onBlur={() => void saveAll()}
        />
        <NumberRow
          title="工具输出修剪阈值（字符）"
          desc="压缩前先把超长的工具输出做首尾截断，避免单个大 output 撑爆上下文。0 = 关闭（零干预）；建议 8192 左右。"
          value={pruneThreshold} min={0} step={512}
          onChange={(v) => { dirtyRef.current = true; setPruneThreshold(v >= 0 ? v : 0); }}
          onBlur={() => void saveAll()}
        />
        <NumberRow
          title="修剪保留头部（字符）"
          desc="超阈值时保留的开头字符数，建议 4096。"
          value={pruneHead} min={0} step={512}
          onChange={(v) => { dirtyRef.current = true; setPruneHead(v >= 0 ? v : 0); }}
          onBlur={() => void saveAll()}
        />
        <NumberRow
          title="修剪保留尾部（字符）"
          desc="超阈值时保留的结尾字符数（多为结果/报错关键信息），建议 1024。"
          value={pruneTail} min={0} step={256}
          onChange={(v) => { dirtyRef.current = true; setPruneTail(v >= 0 ? v : 0); }}
          onBlur={() => void saveAll()}
        />
        <NumberRow
          title="重复失败提醒次数"
          desc="同一工具连续失败达该次数后，下一次请求自动注入一条「换个方案」的提醒（只注入一次）。0 = 关闭，默认 3。"
          value={repeatGuard} min={0}
          onChange={(v) => { dirtyRef.current = true; setRepeatGuard(v >= 0 ? v : 0); }}
          onBlur={() => void saveAll()}
        />
        <NumberRow
          title="无进展提醒次数"
          desc="同一工具连续返回完全相同结果达该次数后，自动注入一条「换参数 / 换工具 / 换路径」的提醒（不论成败，只注入一次）。受「重复失败提醒次数」主开关约束：该项为 0 时本项一并失效。0 = 关闭，默认 3。"
          value={noProgressGuard} min={0}
          onChange={(v) => { dirtyRef.current = true; setNoProgressGuard(v >= 0 ? v : 0); }}
          onBlur={() => void saveAll()}
        />
      </Group>

      <StatusLine saved={saved} error={error} />
    </Box>
  );
}
