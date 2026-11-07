// ── 监控侧边栏（Debug → 监控）：与记忆面板同一套可折叠分组树 ──────────────
// 只读聚合自 /api/debug/memory/metrics，不在前端推算业务结论。
import { useCallback, useEffect, useMemo, useState } from "react";
import { Box, Stack, Tooltip, Typography } from "@mui/material";
import ScrollArea from "../ScrollArea.tsx";
import { debugApi, type MetricsReport } from "../../api/debugApi";
import { Empty, GroupLabel, KV, Pre, Row, Section, StatusBar, TinyBar, Toolbar, num, pct } from "./shared";

export default function MetricsPanel() {
  const [data, setData] = useState<MetricsReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");

  const load = useCallback(() => {
    setLoading(true);
    debugApi.metrics()
      .then((r) => { setData(r.data); if (!r.data.ok) setErr(r.data.error || "读取失败"); })
      .catch((e: unknown) => setErr(String(e)))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 20000);
    return () => clearInterval(t);
  }, [load]);

  const obs = data?.observation;
  const daily = obs?.daily || [];
  const maxRuns = Math.max(1, ...daily.map((d) => d.runs));
  const byTool = useMemo(() => Object.entries(obs?.by_tool || {}).sort((a, b) => b[1] - a[1]), [obs]);
  const maxTool = Math.max(1, ...byTool.map(([, v]) => v));
  const eff = data?.effect_report;
  const tel = data?.telemetry;
  const verdict = eff?.p0_regression?.verdict || {};

  return (
    <Box sx={{ display: "flex", flexDirection: "column", height: "100%", minHeight: 0 }}>
      <Toolbar title="metrics" onRefresh={load} loading={loading} />
      {!!err && <Typography sx={{ px: 1, py: 0.3, fontSize: 10.5, color: "error.main" }} noWrap>{err}</Typography>}
      <ScrollArea sx={{ flexGrow: 1, minHeight: 0 }}>
        <Box sx={{ pb: 1 }}>
          <Section title="online observation" count={obs?.runs ?? "—"} defaultOpen>
            <Row label="观测 run 数" value={obs?.runs ?? "—"} />
            <Row label="检索命中率" value={pct(obs?.hit_rate)}
              tone={(obs?.hit_rate ?? 1) >= 0.5 ? "ok" : "warn"} />
            <Row label="0 召回率" value={pct(obs?.zero_rate)}
              tone={(obs?.zero_rate ?? 1) <= 0.1 ? "ok" : "warn"} />
            <Row label="平均注入字符" value={num(obs?.avg_context_chars, 1)} />
            <Row label="叙事跨场合" value={obs?.narrative_cross ?? "—"} />
            <Row label="外部来源 run" value={obs?.external_runs ?? "—"} />
            <Row label="有效事实原子" value={data?.store?.active_atoms ?? "—"} />
            <Row label="取用总次数" value={data?.store?.total_access ?? "—"} />
            <Row label="月分片 / 最近" value={`${obs?.files ?? 0} / ${obs?.last_ts || "—"}`} />
          </Section>

          <Section title="30d trend" count={`${maxRuns}/天峰值`}>
            {!daily.length ? <Empty text="暂无观测数据" /> : (
              <Box sx={{ px: 0.75, py: 0.5 }}>
                <Stack direction="row" alignItems="flex-end" spacing="2px" sx={{ height: 44 }}>
                  {daily.map((d) => (
                    <Tooltip key={d.date}
                      title={`${d.date} · ${d.runs} run / ${d.calls} 调用 / ${d.hits} 命中 / ${d.zero} 空召回`}>
                      <Box sx={{
                        flex: 1, minWidth: 2, position: "relative",
                        height: d.runs ? `${Math.max(8, (d.runs / maxRuns) * 100)}%` : 3,
                        bgcolor: d.runs ? "primary.main" : "action.hover",
                      }}>
                        {d.zero > 0 && (
                          <Box sx={{
                            position: "absolute", bottom: 0, left: 0, right: 0,
                            height: `${Math.min(100, (d.zero / Math.max(1, d.runs)) * 100)}%`,
                            bgcolor: "error.main", opacity: 0.7,
                          }} />
                        )}
                      </Box>
                    </Tooltip>
                  ))}
                </Stack>
                <Stack direction="row" sx={{ mt: 0.4 }}>
                  <Typography sx={{ fontSize: 10, color: "text.disabled" }}>{daily[0]?.date}</Typography>
                  <Box sx={{ flex: 1 }} />
                  <Typography sx={{ fontSize: 10, color: "text.disabled" }}>
                    {daily[daily.length - 1]?.date}
                  </Typography>
                </Stack>
                <Typography sx={{ fontSize: 10, color: "text.disabled", mt: 0.3 }}>
                  柱高 = run 数，红段 = 0 召回 run
                </Typography>
              </Box>
            )}
          </Section>

          <Section title="tool calls" count={byTool.length}>
            {!byTool.length ? <Empty text="暂无工具调用记录" /> :
              byTool.map(([k, v]) => (
                <Box key={k}>
                  <Row label={k} value={v} />
                  <TinyBar value={v} max={maxTool} />
                </Box>
              ))}
          </Section>

          <Section title="effect report" count={eff?.available ? "已生成" : "未生成"}>
            {!eff?.available ? (
              <Box>
                <Empty text={eff?.note || eff?.error || "尚未生成效果报告"} />
                <Typography sx={{ px: 1.5, pb: 0.6, fontSize: 10.5, color: "text.disabled" }}>
                  执行 <code>python scripts/verify_memory_effect.py</code> 后刷新；报告只落本机 .codebuddy/tmp。
                </Typography>
              </Box>
            ) : (
              <Box>
                <Row label="生成时间" value={eff.generated || "—"} />
                <Row label="L1 位次增益"
                  value={String((eff.l1 as Record<string, unknown>)?.context_rank_gain_mean ?? "—")} />
                <Row label="L1 命中率 A/B"
                  value={`${String((eff.l1 as Record<string, unknown>)?.l1_hit_rate_a ?? "—")} / ${String((eff.l1 as Record<string, unknown>)?.l1_hit_rate_b ?? "—")}`} />
                <Row label="陈旧日记命中率"
                  value={String((eff.narrative as Record<string, unknown>)?.stale_diary_hit_rate ?? "—")} />
                <Row label="叙事自取率(代理)"
                  value={String((eff.narrative_self_fetch as Record<string, unknown>)?.narrative_self_fetch_rate ?? "—")} />
                <Row label="静默成本"
                  value={String((eff.silent_cost as Record<string, unknown>)?.context_block_chars ?? "—")} />
                <GroupLabel>P0 回归不劣化</GroupLabel>
                {Object.entries(verdict).map(([k, v]) => (
                  <Row key={k} label={k} value={v ? "通过" : "劣化"} tone={v ? "ok" : "error"} />
                ))}
                {!Object.keys(verdict).length && <Empty text="无基线留档，只记录现值" />}
                <GroupLabel>待人工层</GroupLabel>
                <KV data={eff.pending_human || {}} />
              </Box>
            )}
          </Section>

          <Section title="telemetry" count={tel?.runs ?? 0}>
            {!tel?.runs ? <Empty text="暂无运行报告" /> : (
              <Box>
                {Object.entries(tel.averages || {}).map(([k, v]) => (
                  <Row key={k} label={k} value={num(v, 3)} />
                ))}
                <GroupLabel>最近 run</GroupLabel>
                {(tel.recent || []).slice(0, 6).map((r, i) => (
                  <Row key={i} label={String(r.run_id || "—")}
                    value={`${r.success ? "成功" : "失败"} · ${r.steps ?? "—"} 步`}
                    tone={r.success ? "ok" : "error"} />
                ))}
              </Box>
            )}
          </Section>

          <Section title="queue / store" count={data?.store?.active_atoms ?? "—"}>
            <KV data={{ ...(data?.writer || {}), ...(data?.store || {}) }} />
          </Section>

          <Section title="notes">
            <Pre maxHeight={180} text={[
              "· hit_rate = hits / calls；zero_rate = 0 召回 run 占比。",
              "· 观测是量尺不是记忆：不进注入面、不给 agent 查询工具。",
              "· P0 四项相对 M0 基线不显著劣化。",
              "· L2 真机 AB 与 L3 人工抽样由主人执行，脚本只给代理值。",
              "· 首要尺子：共同回忆是否有质感；工程指标只作回归保护。",
            ].join("\n")} />
          </Section>
        </Box>
      </ScrollArea>
      <StatusBar>
        <Box sx={{ width: 6, height: 6, borderRadius: "50%", bgcolor: err ? "error.main" : "success.main" }} />
        <Typography sx={{ fontSize: 10, color: "text.disabled", flex: 1 }} noWrap>
          {err ? "读取失败" : `${obs?.runs ?? 0} run · 20s 刷新`}
        </Typography>
        <Typography sx={{ fontSize: 10, color: "text.disabled" }} noWrap>
          {data?.generated || ""}
        </Typography>
      </StatusBar>
    </Box>
  );
}
