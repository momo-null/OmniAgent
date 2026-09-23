import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Vite 配置：dev 端口 5173，将 /api 与 /v1 代理到后端 8000
export default defineConfig({
  plugins: [react()],
  resolve: {
    extensions: [".tsx", ".ts", ".jsx", ".js", ".mjs", ".json"],
  },
  server: {
    port: 5173,
    strictPort: true, // 端口被占用时直接报错退出，而非静默自增到 5174
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
      "/v1": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
});
