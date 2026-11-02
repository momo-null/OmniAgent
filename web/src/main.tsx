import React from "react";
import ReactDOM from "react-dom/client";
import { ThemeProvider } from "@mui/material/styles";
import CssBaseline from "@mui/material/CssBaseline";
import App from "./App";
import theme from "./theme/theme";
import { applyAppearance, loadAppearance } from "./theme/appearance";
import "./index.css";

// 先应用本机显示偏好再挂载：避免首屏按默认字号渲染后跳一下
applyAppearance(loadAppearance());

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <ThemeProvider theme={theme}>
      <CssBaseline />
      <App />
    </ThemeProvider>
  </React.StrictMode>
);
