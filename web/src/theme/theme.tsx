import { createTheme } from "@mui/material/styles";
import type { PaletteColor } from "@mui/material/styles";
import {
  ChevronDownIcon,
  CircleAlertIcon,
  CircleCheckIcon,
  CircleDotIcon,
  CircleIcon,
  InfoIcon,
  SquareCheckIcon,
  SquareIcon,
  SquareMinusIcon,
  TriangleAlertIcon,
  XIcon,
} from "../components/icons";

// 产物类型色是全站仅有的两处色相（其余层次都靠亮度差）。收进 palette 后，
// 做亮色主题只改这一处，不必再去三处图标里翻硬编码 hex。
declare module "@mui/material/styles" {
  interface Palette {
    fileImage: PaletteColor;
    fileDoc: PaletteColor;
  }
  interface PaletteOptions {
    fileImage?: Partial<PaletteColor>;
    fileDoc?: Partial<PaletteColor>;
  }
  interface TypeBackground {
    /** 左侧会话栏底色：比内容区亮一档，靠亮度差分层（用户口径：保留这个默认暗色值） */
    sidebar?: string;
  }
}

// 全局 MUI 主题
const theme = createTheme({
  palette: {
    mode: "dark",
    // 中性灰阶暗色：全站无色相，层次靠亮度差而非色相。
    // 背景两级：内容区 #121212（深）、侧栏与卡片 #1c1c1c（浅）。
    // primary 与 text.primary 同色 ⇒ 标题/正文/强调同源；按钮用 contrastText 深字压在亮底上。
    primary: { main: "#e0e0e0", contrastText: "#121212" },
    secondary: { main: "#9a9a9a" },
    background: { default: "#121212", paper: "#1c1c1c", sidebar: "#2e2e2e" },
    divider: "#2e2e2e",
    fileImage: { main: "#42a5f5", contrastText: "#121212" },
    fileDoc: { main: "#26c6da", contrastText: "#121212" },
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
  // MUI 组件自带的内置图标也换成同一套 Lucide 字形，否则全站只剩这几处还是方的实心图标。
  // 尺寸契约按各组件源码来，不要写死：
  //   Select  —— IconComponent 收的是组件，SelectInput 用 styled 的 `as` 渲染，
  //              ownerState 被 MUI 的 shouldForwardProp 滤掉，只落到 className。
  //   Checkbox/Radio —— 组件会 cloneElement 把 size 当 fontSize 塞进来，所以这里不能写 fontSize，
  //              写了就吃掉调用点的 size="small"。
  //   Alert —— 严重度图标外层 .MuiAlert-icon 是 fontSize: 22，故用 "inherit" 跟原来一致；
  //              关闭图标是 slot（不是节点 prop），组件自己会传 fontSize="small"。
  components: {
    MuiSelect: {
      defaultProps: { IconComponent: ChevronDownIcon },
    },
    MuiCheckbox: {
      defaultProps: {
        icon: <SquareIcon />,
        checkedIcon: <SquareCheckIcon />,
        indeterminateIcon: <SquareMinusIcon />,
      },
    },
    MuiRadio: {
      defaultProps: {
        icon: <CircleIcon />,
        checkedIcon: <CircleDotIcon />,
      },
    },
    MuiAlert: {
      defaultProps: {
        iconMapping: {
          error: <CircleAlertIcon fontSize="inherit" />,
          warning: <TriangleAlertIcon fontSize="inherit" />,
          success: <CircleCheckIcon fontSize="inherit" />,
          info: <InfoIcon fontSize="inherit" />,
        },
        slots: { closeIcon: XIcon },
      },
    },
  },
});

export default theme;
