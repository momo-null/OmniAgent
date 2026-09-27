import { createTheme } from "@mui/material/styles";

// 全局 MUI 主题
const theme = createTheme({
  palette: {
    mode: "dark",
    primary: { main: "#5c6bc0" },
    secondary: { main: "#26a69a" },
    background: { default: "#0f1117", paper: "#1a1d27" },
  },
  typography: {
    // 缩小整体字号：基准 16px -> 14px（根字号由 index.css html{font-size:14px} 配套设定）
    htmlFontSize: 14,
    fontFamily:
      '"Inter", "Roboto", "Helvetica", "Arial", "PingFang SC", "Microsoft YaHei", sans-serif',
    // 全局上限 16px（2026-09-27 用户口径：除左上角标题外不得有 >16px；根字号生效后
    // body1/subtitle1=16px 恰好达标，仅 h 系列默认 >16 需封顶。h1-h5 当前未使用，一并封顶防回潮。
    // 左上角标题（Layout subtitle1）=16px 加粗，为页面最大字号。
    h6: { fontSize: "1.1429rem" },      // 16px（DialogTitle / AppBar 等）
    h5: { fontSize: "1.1429rem" },      // 16px
    h4: { fontSize: "1.1429rem" },      // 16px
    h3: { fontSize: "1.1429rem" },      // 16px
    h2: { fontSize: "1.1429rem" },      // 16px
    h1: { fontSize: "1.1429rem" },      // 16px
  },
});

export default theme;
