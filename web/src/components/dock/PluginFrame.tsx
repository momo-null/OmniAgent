// 插件页容器（阶段 6）：宿主只画壳——标题 + 状态 + sandbox iframe。
// 页面 100% 由插件服务自己出（不经主后端代管、不解析页面语义）；
// 状态文字只来自 harness 落盘的 sources 快照，不探测、不猜。
import { useCallback, useMemo, useState } from "react";
import { Box, IconButton, Stack, Tooltip, Typography } from "@mui/material";
import { useTheme } from "@mui/material/styles";
import { RefreshCwIcon } from "../icons";
import type { SourceState } from "../../api/client.ts";

function pageSrc(url: string, themeMode: string, stamp: number): string {
  try {
    const u = new URL(url);
    u.searchParams.set("theme", themeMode);
    if (stamp) u.searchParams.set("_", String(stamp));
    return u.toString();
  } catch {
    const sep = url.includes("?") ? "&" : "?";
    return `${url}${sep}theme=${encodeURIComponent(themeMode)}${stamp ? `&_=${stamp}` : ""}`;
  }
}

const STATE_LABEL: Record<string, string> = {
  connected: "已连接",
  starting: "启动中",
  disabled: "已停用",
  failed: "失败",
  stopped: "未运行",
};
const STATE_COLOR: Record<string, string> = {
  connected: "success.main",
  starting: "warning.main",
  disabled: "text.disabled",
  failed: "error.main",
  stopped: "text.disabled",
};

function statusText(src?: SourceState, note?: string): string {
  if (!src) return note || "插件页已加载（事件源状态未报告）";
  const base = STATE_LABEL[src.state] || src.state;
  if (src.state === "connected" && src.probe && src.probe !== "unknown") {
    return src.probe === "ready" ? `${base} · 就绪` : `${base} · 未就绪`;
  }
  return src.error ? `${base}：${src.error}` : base;
}

export default function PluginFrame({
  label, url, source, note,
}: {
  label: string;
  url: string;
  source?: SourceState;
  note?: string;
}) {
  const { palette: { mode: themeMode } } = useTheme();
  const [loadedAt, setLoadedAt] = useState(0);
  const reload = useCallback(() => {
    // 加时间戳绕过缓存：插件服务自己的页面不该被宿主缓存
    setLoadedAt(Date.now());
  }, []);
  const src = useMemo(() => pageSrc(url, themeMode, loadedAt), [url, themeMode, loadedAt]);
  const text = statusText(source, note);
  const color = STATE_COLOR[source?.state || "stopped"] || "text.secondary";
  // 页面归插件服务，不归事件源状态：只要 manifest 给了 URL 就渲染；
  // 事件源状态只用于标题旁的状态文字（没有 events 面的工具插件不报告它）。
  const blocked = !url;
  const reason = !url
    ? "该插件未在 manifest 里声明页面地址（ui.url）。"
    : "";

  return (
    <Box sx={{ height: "100%", minHeight: 0, display: "flex", flexDirection: "column" }}>
      <Stack direction="row" alignItems="center" spacing={1}
             sx={{ px: 1.25, py: 0.5, flexShrink: 0, borderBottom: "1px solid", borderColor: "divider" }}>
        <Typography variant="caption" sx={{ fontWeight: 500, minWidth: 0 }} noWrap>{label}</Typography>
        <Tooltip title={text} placement="bottom">
          <Typography variant="caption" noWrap
                      sx={{ color, maxWidth: 260, overflow: "hidden", textOverflow: "ellipsis", fontSize: 11 }}>
            {text}
          </Typography>
        </Tooltip>
        <Box sx={{ flex: 1 }} />
        <Tooltip title="重新加载页面">
          <IconButton size="small" onClick={reload} aria-label="重新加载插件页"
                      sx={{ width: 24, height: 24 }}>
            <RefreshCwIcon sx={{ fontSize: 15 }} />
          </IconButton>
        </Tooltip>
      </Stack>
      {blocked ? (
        // 服务不可用时不留空白 iframe：把原因说清楚（判据 7 的「自动变红并给出原因」）
        <Box sx={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", p: 3 }}>
          <Typography variant="body2" color="text.secondary" sx={{ textAlign: "center", maxWidth: 420 }}>
            {reason}
          </Typography>
        </Box>
      ) : (
        <Box sx={{ flex: 1, minHeight: 0 }}>
          <Box
            component="iframe"
            src={src}
            title={label}
            // 插件页的 API 就在它自己的源上（:18751）。不给 allow-same-origin 会让文档
            // 变成 opaque origin，连自己的 /api/* 都算跨源 → 浏览器直接 Failed to fetch。
            // 这个同源只覆盖插件服务本身，不等于把宿主的源交给它。
            sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-downloads"
            referrerPolicy="no-referrer"
            sx={{ width: "100%", height: "100%", border: 0, display: "block", bgcolor: "background.paper" }}
          />
        </Box>
      )}
    </Box>
  );
}
