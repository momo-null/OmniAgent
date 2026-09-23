import { Component, ReactNode } from "react";
import { Box, Alert } from "@mui/material";
import { TaskStoreProvider } from "./store/taskStore.tsx";
import Layout from "./components/Layout";

// 轻量错误边界：渲染异常时显示错误而非整页白屏（便于排错）
class ErrorBoundary extends Component<
  { children: ReactNode },
  { error: Error | null }
> {
  state: { error: Error | null } = { error: null };
  static getDerivedStateFromError(error: Error) {
    return { error };
  }
  render() {
    if (this.state.error) {
      return (
        <Box sx={{ p: 4 }}>
          <Alert severity="error">
            页面渲染出错（已捕获，未白屏）：{this.state.error.message}
          </Alert>
        </Box>
      );
    }
    return this.props.children;
  }
}

export default function App() {
  return (
    <TaskStoreProvider>
      <ErrorBoundary>
        <Layout />
      </ErrorBoundary>
    </TaskStoreProvider>
  );
}
