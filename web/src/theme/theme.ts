import { createTheme } from "@mui/material/styles";

// 全局 MUI 主题
const theme = createTheme({
  palette: {
    mode: "dark",
    // 中性灰阶暗色：全站无色相，层次靠亮度差而非色相。
    // 背景两级：内容区 #121212（深）、侧栏与卡片 #1c1c1c（浅）。
    // primary 与 text.primary 同色 ⇒ 标题/正文/强调同源；按钮用 contrastText 深字压在亮底上。
    primary: { main: "#e0e0e0", contrastText: "#121212" },
    secondary: { main: "#9a9a9a" },
    background: { default: "#121212", paper: "#1c1c1c" },
    divider: "#2e2e2e",
    // 半透明叠加层（气泡/胶囊/hover）随背景走，保持比底色亮一档
    action: {
      hover: "rgba(255,255,255,0.06)",
      selected: "rgba(255,255,255,0.10)",
    },
    // 正文对比度约 13:1、次要约 7.5:1（原 MUI dark 默认纯白在近黑底上约 18.5:1，刺眼）
    text: {
      primary: "#e0e0e0",
      secondary: "#9a9a9a",
      disabled: "#6b6b6b",
    },
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
