import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 后端地址：开发期由 Vite 代理 /api 到 FastAPI（默认 8000）
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8000",
    },
  },
});
