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
    // 缩小整体字号：基准 16px -> 14px，所有基于 rem 的 MUI 文字同比缩小
    htmlFontSize: 14,
    fontFamily:
      '"Inter", "Roboto", "Helvetica", "Arial", "PingFang SC", "Microsoft YaHei", sans-serif',
  },
});

export default theme;
