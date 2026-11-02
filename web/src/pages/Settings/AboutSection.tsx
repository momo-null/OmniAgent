import { Box, Paper, Typography } from "@mui/material";
import { Group } from "../../components/SettingsKit";

// 关于：内核形态说明 + 「哪条配置在哪个执行路径生效」。这两张卡是防误配的说明，
// 不是可操作项，所以保持正文形态而不是塞进设置行。
export default function AboutSection() {
  return (
    <Box>
      <Group title="关于">
        <Typography variant="body2" color="text.secondary" sx={{ lineHeight: 1.8 }}>
          OmniAgent · 通用 agent 编排内核（Plan C：project + task 两实体）。<br />
          主 agent 规划并执行；可在模型页为多个槽位配置模型，主 agent 自主决定派发；知识库自学（world-model / skill）。<br />
          内核零场景硬编码；能力来自工具与运行时自学。
        </Typography>
      </Group>

      <Group title="执行路径与配置生效范围" desc="引擎有两条执行路径，同一份配置里只有部分键在对应路径生效——别混：">
        <Paper variant="outlined" sx={{ mb: 2, p: 2 }}>
          <Typography variant="subtitle2">① 分层形态（模型页给 worker 槽位选了模型）</Typography>
          <Typography variant="body2" color="text.secondary" component="div">
            主模型规划 + 子模型执行，支持派发多 agent。<br />
            生效配置：
            <ul style={{ margin: "4px 0", paddingLeft: 20 }}>
              <li>通用 · 默认步数上限 → 任务<b>总预算</b>（硬上限，耗尽即停）</li>
              <li>编排 · 派发最大轮次 / 并发 → 控制派发与回收</li>
              <li>编排 · 预算提示触发比例 → 接近预算时给 agent 陈述提示</li>
              <li>编排 · 分块步数 → SDK 单次内部步数块大小（检查点粒度）</li>
            </ul>
            <b>brain.long_task.* 在此路径不生效。</b>
          </Typography>
        </Paper>

        <Paper variant="outlined" sx={{ p: 2 }}>
          <Typography variant="subtitle2">② 单主 agent 形态（worker 槽位未选模型时）</Typography>
          <Typography variant="body2" color="text.secondary" component="div">
            主模型自己跑完整任务，无子 agent。<br />
            生效配置：
            <ul style={{ margin: "4px 0", paddingLeft: 20 }}>
              <li>通用 · 默认步数上限 → 仍是<b>总预算</b>（硬上限）</li>
              <li>编排 · 长任务压缩 → 压缩时是否调用主模型生成中文摘要</li>
              <li>编排 · 压缩触发占比 → 触发压缩的上下文占比（0 = 取默认 0.5）</li>
              <li>编排 · 模型上下文上限 → 填 0 即关闭粘性压缩（真正的开关）</li>
            </ul>
            <b>编排 · 派发轮次 / 并发在此形态仍生效</b>：派发由主模型自派发执行（并发与上下文隔离仍有价值）。
          </Typography>
        </Paper>
      </Group>
    </Box>
  );
}
