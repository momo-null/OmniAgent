// 右侧栏「浏览器」页：内嵌 iframe 预览（localhost 服务 / 文档等）。
// 部分站点通过 X-Frame-Options 拒绝被内嵌——提供「系统浏览器打开」兜底。
import { useEffect, useState } from "react";
import { Box, IconButton, InputBase, Stack, Tooltip, Typography } from "@mui/material";
import OpenInNewOutlined from "@mui/icons-material/OpenInNewOutlined";
import PlayArrowOutlined from "@mui/icons-material/PlayArrowOutlined";
import RefreshOutlined from "@mui/icons-material/RefreshOutlined";

const URL_KEY = "omni.dock.browserUrl";

function normalizeUrl(raw: string): string {
  const s = raw.trim();
  if (!s) return "";
  // 无协议输入默认按 http 处理（localhost:8000 → http://localhost:8000）
  return /^https?:\/\//i.test(s) ? s : `http://${s}`;
}

export default function BrowserTab() {
  const [input, setInput] = useState(() => localStorage.getItem(URL_KEY) || "");
  const [url, setUrl] = useState(() => normalizeUrl(localStorage.getItem(URL_KEY) || ""));

  useEffect(() => {
    localStorage.setItem(URL_KEY, input);
  }, [input]);

  const go = () => setUrl(normalizeUrl(input));

  return (
    <Box sx={{ display: "flex", flexDirection: "column", minHeight: 0, height: "100%" }}>
      <Stack direction="row" spacing={0.5} alignItems="center"
        sx={{ px: 1, py: 0.75, flexShrink: 0, borderBottom: "1px solid", borderColor: "divider" }}>
        <InputBase
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") go(); }}
          placeholder="要打开的地址，如 localhost:8000/docs"
          size="small"
          sx={{
            flex: 1, minWidth: 0, fontSize: 13, px: 1,
            bgcolor: "action.hover", borderRadius: 1, height: 30,
          }}
        />
        <Tooltip title="打开">
          <IconButton size="small" onClick={go} title="打开">
            <PlayArrowOutlined fontSize="small" />
          </IconButton>
        </Tooltip>
        <Tooltip title="刷新">
          <IconButton size="small" title="刷新" onClick={() => setUrl((u) => (u ? `${u.split("#")[0]}#${Date.now()}` : u))}>
            <RefreshOutlined fontSize="small" />
          </IconButton>
        </Tooltip>
        <Tooltip title="在系统浏览器打开">
          <IconButton size="small" title="在系统浏览器打开"
            onClick={() => url && window.open(url, "_blank")}>
            <OpenInNewOutlined fontSize="small" />
          </IconButton>
        </Tooltip>
      </Stack>
      {url ? (
        <Box
          component="iframe"
          // 本地工具页（agent 起的服务 / 文档站），同源策略按宿主浏览器默认处理
          src={url}
          title="浏览器预览"
          sx={{ flexGrow: 1, minHeight: 0, width: "100%", border: "none", bgcolor: "#fff" }}
        />
      ) : (
        <Box sx={{ flexGrow: 1, display: "flex", alignItems: "center", justifyContent: "center", p: 3 }}>
          <Typography variant="body2" color="text.secondary" sx={{ textAlign: "center" }}>
            输入地址后打开内嵌浏览器。
            <br />
            适合查看 agent 启动的本地服务 / 页面；
            <br />
            部分站点禁止被内嵌，可用右上角按钮在系统浏览器打开。
          </Typography>
        </Box>
      )}
    </Box>
  );
}
