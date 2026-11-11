// ── 记忆侧边栏（Debug → 记忆）：桌面插件风格的可折叠分组树 ──────────────────
// 无横向 Tab、无卡片网格、无全屏放大；所有层级在一条纵向滚动流里展开。
// 数据只读聚合自 /api/debug/memory/*；纠正 / 遗忘 / 删除复用 memory_brain 原语。
import { useCallback, useEffect, useMemo, useState } from "react";
import { Box, Button, IconButton, MenuItem, Stack, TextField, Typography } from "@mui/material";
import { DeleteOutline } from "@mui/icons-material";
import ScrollArea from "../ScrollArea.tsx";
import {
  debugApi, type DocumentText, type MemoryAtomRow, type MemoryDocument,
  type MemoryLandscape, type NarrativeRow, type PageResponse,
} from "../../api/debugApi";
import { Empty, GroupLabel, KV, Pre, Row, Section, StatusBar, TinyBar, Toolbar, num } from "./shared";

const PAGE = 50;

/** 展示年龄（关系卡冻结退役的可见化：7 天窗外停更，不删——「最后活跃 N 天前」）。 */
function ageOf(iso: string): string {
  if (!iso) return "—";
  const ms = Date.now() - new Date(iso).getTime();
  const days = Math.floor(ms / 86400000);
  if (days >= 1) return `${days}天前`;
  const hours = Math.floor(ms / 3600000);
  return hours >= 1 ? `${hours}小时前` : "刚刚";
}

function docKeyFor(row: NarrativeRow, docs: MemoryDocument[]): string {
  const id = row.doc_id.split(":").pop() || row.doc_id;
  const hit = docs.find((d) => d.kind === row.kind &&
    (d.name === id || d.name === `${id}.md` || d.key.endsWith(`/${id}`) || d.key.endsWith(`/${id}.md`)));
  return hit?.key || (row.kind === "relation" ? `relation/${id}.md` : `${row.kind}/${id}`);
}

// speaker 空是设计：只有群消息行首「名片(uid): 」才写值，Web 私聊的 user 是主人本人。
function l0Speaker(r: Record<string, unknown>): string {
  const sp = String(r.speaker || "").trim();
  if (sp) return sp;
  const role = String(r.role || "");
  const src = String(r.source_kind || "");
  if (role === "assistant") return "助手本人";
  if (role === "system") return src === "observation" ? "观察记录" : "系统";
  if (src === "task") return "任务输入（主人）";
  const ck = String(r.context_key || "");
  if (ck.startsWith("web:")) return "主人（Web 会话）";
  if (ck) return "通道 user（无名片）";
  return "主人（Web 会话）";
}

function localTime(v: unknown): string {
  const s = String(v || "");
  if (!s) return "";
  const d = new Date(s);
  return Number.isNaN(d.getTime()) ? s : d.toLocaleString();
}

function DocInline({ docKey, onClose }: { docKey: string; onClose: () => void }) {
  const [text, setText] = useState<DocumentText | null>(null);
  useEffect(() => {
    setText(null);
    debugApi.readDocument(docKey).then((r) => setText(r.data))
      .catch((e: unknown) => setText({ ok: false, error: String(e) }));
  }, [docKey]);
  return (
    <Box>
      <Row label={`↳ ${docKey}`} value="收起" onClick={onClose} />
      {!text ? <Empty text="读取中…" /> :
        !text.ok ? <Empty text={text.error || "读取失败"} /> :
          <Pre text={text.text || ""} maxHeight={240} />}
    </Box>
  );
}

function SearchLine({ value, onSearch, placeholder }: {
  value: string; onSearch: (v: string) => void; placeholder: string;
}) {
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);
  return (
    <Stack direction="row" spacing={0.5} sx={{ px: 0.75, pb: 0.4 }}>
      <TextField size="small" fullWidth value={draft} placeholder={placeholder}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter") onSearch(draft.trim()); }}
        inputProps={{ style: { fontSize: 11, padding: "4px 6px" } }} />
      <Button size="small" sx={{ minWidth: 32, fontSize: 11 }}
        onClick={() => onSearch(draft.trim())}>查</Button>
    </Stack>
  );
}

function Pager({ offset, total, limit, onMove }: {
  offset: number; total: number; limit: number; onMove: (next: number) => void;
}) {
  return (
    <Stack direction="row" alignItems="center" spacing={0.5} sx={{ px: 0.75, py: 0.3 }}>
      <Button size="small" disabled={offset <= 0} sx={{ fontSize: 10, minWidth: 24 }}
        onClick={() => onMove(Math.max(0, offset - limit))}>‹</Button>
      <Typography sx={{ fontSize: 10, color: "text.disabled", flex: 1, textAlign: "center" }}>
        {total ? `${offset + 1}–${Math.min(offset + limit, total)} / ${total}` : "0 条"}
      </Typography>
      <Button size="small" disabled={offset + limit >= total} sx={{ fontSize: 10, minWidth: 24 }}
        onClick={() => onMove(offset + limit)}>›</Button>
    </Stack>
  );
}

export default function MemoryPanel() {
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [land, setLand] = useState<MemoryLandscape | null>(null);
  const [atoms, setAtoms] = useState<PageResponse<MemoryAtomRow> | null>(null);
  const [l0, setL0] = useState<PageResponse<Record<string, unknown>> | null>(null);
  const [narr, setNarr] = useState<PageResponse<NarrativeRow> | null>(null);
  const [docs, setDocs] = useState<MemoryDocument[]>([]);

  const [atomOffset, setAtomOffset] = useState(0);
  const [atomState, setAtomState] = useState("");
  const [atomKind, setAtomKind] = useState("");
  const [atomQ, setAtomQ] = useState("");
  const [l0Offset, setL0Offset] = useState(0);
  const [l0Q, setL0Q] = useState("");
  const [narrKind, setNarrKind] = useState("");
  const [narrQ, setNarrQ] = useState("");

  const [openAtom, setOpenAtom] = useState("");
  const [openL0, setOpenL0] = useState("");
  const [openDoc, setOpenDoc] = useState("");
  const [correctFor, setCorrectFor] = useState("");
  const [correctText, setCorrectText] = useState("");
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(() => {
    setLoading(true);
    const jobs: Promise<unknown>[] = [
      debugApi.landscape().then((r) => {
        setLand(r.data);
        if (!r.data.ok) setErr(r.data.error || "读取失败");
      }).catch((e: unknown) => setErr(String(e))),
      debugApi.documents().then((r) => setDocs(r.data.items || [])).catch(() => {}),
      debugApi.atoms({ limit: PAGE, offset: atomOffset, state: atomState, kind: atomKind, q: atomQ })
        .then((r) => setAtoms(r.data)).catch(() => {}),
      debugApi.l0({ limit: PAGE, offset: l0Offset, q: l0Q })
        .then((r) => setL0(r.data)).catch(() => {}),
      debugApi.narrative({ limit: 200, kind: narrKind, q: narrQ })
        .then((r) => setNarr(r.data)).catch(() => {}),
    ];
    Promise.allSettled(jobs).then(() => setLoading(false));
  }, [atomOffset, atomState, atomKind, atomQ, l0Offset, l0Q, narrKind, narrQ]);

  useEffect(() => {
    setErr("");
    refresh();
    const t = setInterval(refresh, 20000);
    return () => clearInterval(t);
  }, [refresh]);

  const fail = (e: unknown) => {
    setBusy(false);
    const x = e as { response?: { data?: { error?: string } }; message?: string };
    window.alert(`操作失败：${x?.response?.data?.error || x?.message || String(e)}`);
  };
  const afterWrite = () => { setBusy(false); setCorrectFor(""); refresh(); };
  const doCorrect = (rid: string) => {
    if (!correctText.trim()) return;
    setBusy(true);
    debugApi.correctAtom(rid, correctText.trim()).then(afterWrite).catch(fail);
  };
  const doForget = (a: MemoryAtomRow) => {
    if (!window.confirm(`让这条记忆退场（软删，内容保留可溯）？\n\n${a.content.slice(0, 120)}`)) return;
    setBusy(true);
    debugApi.forgetAtom(a.record_id).then(afterWrite).catch(fail);
  };
  const doDelete = (a: MemoryAtomRow) => {
    if (!window.confirm(`物理删除这条记忆？不可恢复。\n\n${a.content.slice(0, 120)}`)) return;
    setBusy(true);
    debugApi.deleteMemory(a.record_id).then(afterWrite).catch(fail);
  };

  const doRetireCard = (d: MemoryDocument) => {
    const uid = d.name.endsWith(".md") ? d.name.slice(0, -3) : d.name;
    if (!window.confirm(`注销关系卡 ${d.name}？删正文 + 撤索引，不可恢复（主人卡除外）。`)) return;
    setBusy(true);
    debugApi.deleteRelationCard(uid).then(afterWrite).catch(fail);
  };

  const kindEntries = useMemo(
    () => Object.entries(land?.brain?.by_kind || {}).sort((a, b) => b[1] - a[1]), [land]);
  const maxKind = Math.max(1, ...kindEntries.map(([, v]) => v));
  const docGroups = useMemo(() => {
    const g: Record<string, MemoryDocument[]> = {};
    for (const d of docs) (g[d.layer] ||= []).push(d);
    return g;
  }, [docs]);
  const toggleDoc = (key: string) => setOpenDoc((v) => v === key ? "" : key);

  return (
    <Box sx={{ display: "flex", flexDirection: "column", height: "100%", minHeight: 0 }}>
      <Toolbar title="memory" onRefresh={refresh} loading={loading} />
      {!!err && (
        <Typography sx={{ px: 1, py: 0.3, fontSize: 10.5, color: "error.main" }} noWrap>{err}</Typography>
      )}
      <ScrollArea sx={{ flexGrow: 1, minHeight: 0 }}>
        <Box sx={{ pb: 1 }}>
          <Section title="overview" count={land?.brain?.total ?? "—"} defaultOpen>
            <Row label="L1 事实原子" value={land?.brain?.total ?? "—"} />
            <Row label="  active / fts" value={`${land?.brain?.active ?? "—"} / ${land?.brain?.atoms_fts ?? "—"}`} />
            <Row label="L0 逐字消息" value={land?.brain?.l0 ?? "—"} />
            <Row label="叙事索引" value={land?.brain?.narrative ?? "—"} />
            <Row label="L2 场景块" value={land?.l2?.documents?.scene ?? "—"} />
            <Row label="圈层语境 / 缓冲" value={`${land?.brain?.circle_notes ?? 0} / ${land?.brain?.context_buffers ?? 0}`} />
            <Row label="正文文件" value={docs.length} />
            <Row label="brain.db" value={`${num((land?.brain?.bytes || 0) / 1024 / 1024)} MB`} />
            <Row label="待写队列" value={num(land?.writer?.pending ?? 0, 0)}
              tone={(land?.writer?.pending as number) > 0 ? "warn" : "ok"} />
            <Row label="FTS 差额 atoms/l0/narr"
              value={`${land?.health?.atoms_fts_gap ?? 0} / ${land?.health?.l0_fts_gap ?? 0} / ${land?.health?.narrative_fts_gap ?? 0}`} />
            <Row label="上次巩固" value={land?.health?.last_consolidation || "—"} />
            <GroupLabel>L1 类别分布</GroupLabel>
            {kindEntries.length ? kindEntries.map(([k, v]) => (
              <Box key={k}>
                <Row label={k} value={v} />
                <TinyBar value={v} max={maxKind} />
              </Box>
            )) : <Empty text="暂无事实原子" />}
          </Section>

          <Section title="L1 · facts" count={atoms?.total ?? "—"} defaultOpen>
            <SearchLine value={atomQ} placeholder="搜事实 / record_id / 来源任务"
              onSearch={(v) => { setAtomQ(v); setAtomOffset(0); }} />
            <Stack direction="row" spacing={0.5} sx={{ px: 0.75, pb: 0.4 }}>
              <TextField select size="small" value={atomState} sx={{ flex: 1 }}
                onChange={(e) => { setAtomState(e.target.value); setAtomOffset(0); }}
                inputProps={{ style: { fontSize: 11, padding: "4px 6px" } }}>
                <MenuItem value="" sx={{ fontSize: 11 }}>全部状态</MenuItem>
                {(land?.brain?.by_state ? Object.keys(land.brain.by_state) : ["active", "tombstone"]).map((s) => (
                  <MenuItem key={s} value={s} sx={{ fontSize: 11 }}>{s}</MenuItem>
                ))}
              </TextField>
              <TextField select size="small" value={atomKind} sx={{ flex: 1 }}
                onChange={(e) => { setAtomKind(e.target.value); setAtomOffset(0); }}
                inputProps={{ style: { fontSize: 11, padding: "4px 6px" } }}>
                <MenuItem value="" sx={{ fontSize: 11 }}>全部类别</MenuItem>
                {kindEntries.map(([k]) => <MenuItem key={k} value={k} sx={{ fontSize: 11 }}>{k}</MenuItem>)}
              </TextField>
            </Stack>
            {!atoms?.items?.length ? <Empty text={loading ? "加载中…" : "没有匹配的事实原子"} /> :
              atoms.items.map((a) => (
                <Box key={a.record_id}>
                  <Row
                    label={a.content}
                    value={`${a.state || "active"} · ${num(a.salience)}`}
                    selected={openAtom === a.record_id}
                    tone={a.state && a.state !== "active" ? "warn" : undefined}
                    onClick={() => setOpenAtom((v) => v === a.record_id ? "" : a.record_id)}
                    title={a.content} />
                  {openAtom === a.record_id && (
                    <Box>
                      <KV data={a} skip={["content"]} />
                      <Stack direction="row" spacing={0.5} sx={{ px: 1, py: 0.4 }}>
                        <Button size="small" disabled={busy} sx={{ fontSize: 10.5 }}
                          onClick={() => { setCorrectFor(a.record_id); setCorrectText(a.content); }}>纠正</Button>
                        <Button size="small" disabled={busy} sx={{ fontSize: 10.5 }}
                          onClick={() => doForget(a)}>遗忘</Button>
                        <Button size="small" disabled={busy} color="error" sx={{ fontSize: 10.5 }}
                          onClick={() => doDelete(a)}>物理删除</Button>
                      </Stack>
                      {correctFor === a.record_id && (
                        <Box sx={{ px: 1, pb: 0.6 }}>
                          <TextField multiline fullWidth minRows={2} value={correctText} disabled={busy}
                            onChange={(e) => setCorrectText(e.target.value)}
                            inputProps={{ style: { fontSize: 11 } }} />
                          <Button size="small" disabled={busy || !correctText.trim()} sx={{ fontSize: 10.5, mt: 0.4 }}
                            onClick={() => doCorrect(a.record_id)}>写入新条（旧条留痕）</Button>
                        </Box>
                      )}
                    </Box>
                  )}
                </Box>
              ))}
            <Pager offset={atomOffset} total={atoms?.total || 0} limit={PAGE} onMove={setAtomOffset} />
          </Section>

          <Section title="L0 · transcript" count={l0?.total ?? "—"}>
            <SearchLine value={l0Q} placeholder="搜逐字原文 / 说话人"
              onSearch={(v) => { setL0Q(v); setL0Offset(0); }} />
            {!l0?.items?.length ? <Empty text={loading ? "加载中…" : "暂无逐字索引"} /> :
              l0.items.map((r, i) => {
                const rid = String(r.record_id || i);
                return (
                  <Box key={rid}>
                    <Row label={String(r.text || "")} value={l0Speaker(r)}
                      selected={openL0 === rid}
                      onClick={() => setOpenL0((v) => v === rid ? "" : rid)}
                      title={String(r.text || "")} />
                    {openL0 === rid && (
                      <Box>
                        <Row label="本地时间" value={localTime(r.recorded_at)} />
                        <KV data={r} skip={["text"]} />
                        <Pre text={String(r.text || "")} maxHeight={160} />
                      </Box>
                    )}
                  </Box>
                );
              })}
            <Pager offset={l0Offset} total={l0?.total || 0} limit={PAGE} onMove={setL0Offset} />
          </Section>

          <Section title="L2 · narrative" count={narr?.total ?? "—"}>
            <Stack direction="row" spacing={0.5} sx={{ px: 0.75, pb: 0.4 }}>
              <TextField select size="small" value={narrKind} sx={{ width: 96 }}
                onChange={(e) => setNarrKind(e.target.value)}
                inputProps={{ style: { fontSize: 11, padding: "4px 6px" } }}>
                <MenuItem value="" sx={{ fontSize: 11 }}>全部</MenuItem>
                {["diary", "month", "relation"].map((k) => (
                  <MenuItem key={k} value={k} sx={{ fontSize: 11 }}>{k}</MenuItem>
                ))}
              </TextField>
              <TextField size="small" fullWidth placeholder="搜摘要 / 主题 / 人物" value={narrQ}
                onChange={(e) => setNarrQ(e.target.value)}
                inputProps={{ style: { fontSize: 11, padding: "4px 6px" } }} />
            </Stack>
            {!narr?.items?.length ? <Empty text={loading ? "加载中…" : "暂无叙事条目"} /> :
              narr.items.map((r) => {
                const key = docKeyFor(r, docs);
                return (
                  <Box key={`${r.kind}/${r.doc_id}`}>
                    <Row label={`${r.doc_id}  ${r.summary || ""}`}
                      value={r.date_from || r.kind}
                      selected={openDoc === key} onClick={() => toggleDoc(key)} />
                    {openDoc === key && <DocInline docKey={key} onClose={() => setOpenDoc("")} />}
                  </Box>
                );
              })}
            <GroupLabel>场景块 / 维护水位</GroupLabel>
            {!(land?.l2?.scenes || []).length ? <Empty text="暂无场景块" /> :
              (land?.l2?.scenes || []).map((s, i) => (
                <Box key={i}>
                  <Row label={String(s.filename || s.project || "水位")}
                    value={String(s.heat ?? s.watermark_last_atom ?? "")} />
                </Box>
              ))}
          </Section>

          <Section title="L3 · profile" count="3">
            {([
              ["profile", "profile/self_profile.md"],
              ["character", "character/character.md"],
              ["agents", "agents/global"],
            ] as const).map(([k, key]) => {
              const a = land?.l3?.[k];
              return (
                <Box key={k}>
                  <Row label={k} value={a?.exists ? `${a.chars} B` : "不存在"}
                    selected={openDoc === key} onClick={() => a?.exists && toggleDoc(key)} />
                  {openDoc === key && <DocInline docKey={key} onClose={() => setOpenDoc("")} />}
                </Box>
              );
            })}
            <GroupLabel>三级纪律 / 场景索引</GroupLabel>
            <Row label="项目级 AGENTS.md" value={docs.filter((d) => d.name.includes("项目 ")).length} />
            <Row label="任务级 AGENTS.md" value={docs.filter((d) => d.name.includes("任务 ")).length} />
            <Row label="场景块" value={docs.filter((d) => d.kind === "scene").length} />
          </Section>

          <Section title="context / queue" count={land?.context?.count ?? "—"}>
            <GroupLabel>圈层语境</GroupLabel>
            {!(land?.context?.notes || []).length ? <Empty text="暂无圈层语境" /> :
              (land?.context?.notes || []).map((n, i) => (
                <Row key={i} label={String(n.summary || n.context_key || "—")}
                  value={`v${n.version ?? 0}`} />
              ))}
            <GroupLabel>水位 / 队列</GroupLabel>
            <KV data={{ ...(land?.brain?.meta || {}), ...(land?.writer || {}) }} />
          </Section>

          <Section title="all documents" count={docs.length}>
            {!Object.keys(docGroups).length ? <Empty text="暂无正文文件" /> :
              Object.entries(docGroups).map(([layer, items]) => (
                <Box key={layer}>
                  <GroupLabel>{layer} · {items.length}</GroupLabel>
                  {items.map((d) => (
                    <Box key={d.key}>
                      {d.kind === "relation" ? (
                        <Stack direction="row" alignItems="center" spacing={0.5}
                          onClick={() => toggleDoc(d.key)} title={d.name}
                          sx={{
                            pl: 1, pr: 0.5, minHeight: 23, cursor: "pointer",
                            borderLeft: "2px solid",
                            borderColor: openDoc === d.key ? "primary.main" : "transparent",
                            bgcolor: openDoc === d.key ? "action.selected" : "transparent",
                            "&:hover": { bgcolor: "action.hover" },
                          }}>
                          <Typography sx={{ fontSize: 10.5, flex: 1, minWidth: 0 }} noWrap>
                            {d.name}
                          </Typography>
                          <Typography sx={{ fontSize: 10, color: "text.disabled" }} noWrap>
                            {ageOf(d.updated)}
                          </Typography>
                          <IconButton size="small" sx={{ p: 0.25 }}
                            title="注销这张卡（删正文+撤索引；主人卡除外）"
                            disabled={busy}
                            onClick={(e) => { e.stopPropagation(); doRetireCard(d); }}>
                            <DeleteOutline fontSize="inherit" />
                          </IconButton>
                        </Stack>
                      ) : (
                        <Row label={d.name} value={`${num(d.chars / 1024)} KB`}
                          selected={openDoc === d.key} onClick={() => toggleDoc(d.key)} title={d.name} />
                      )}
                      {openDoc === d.key && <DocInline docKey={d.key} onClose={() => setOpenDoc("")} />}
                    </Box>
                  ))}
                </Box>
              ))}
          </Section>
        </Box>
      </ScrollArea>
      <StatusBar>
        <Box sx={{ width: 6, height: 6, borderRadius: "50%",
          bgcolor: land?.enabled ? "success.main" : "warning.main" }} />
        <Typography sx={{ fontSize: 10, color: "text.disabled", flex: 1 }} noWrap>
          记忆{land?.enabled ? "开启" : "关闭"} · {land?.brain?.total ?? 0} atoms · 20s 刷新
        </Typography>
        <Typography sx={{ fontSize: 10, color: "text.disabled" }} noWrap>
          {land?.generated || ""}
        </Typography>
      </StatusBar>
    </Box>
  );
}
