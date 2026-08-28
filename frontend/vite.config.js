import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 工程版（src/）开发服务器配置。
// 前端使用相对路径 /api/* 调后端，dev 模式下必须由 Vite 把 /api 代理到 FastAPI，
// 否则浏览器会把请求打到 5173（Vite 自身），导致 404。
// 同时代理 /output/*：mediaSrc() 构造的图片 URL 是相对路径（/output/xxx.png），
// 浏览器请求时若没有代理规则，Vite 会 fallback 到 index.html 导致图片加载失败。
const BACKEND_URL = process.env.BLINDBOX_BE_URL || "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    port: Number(process.env.BLINDBOX_FE_PORT || 5173),
    strictPort: false, // 端口被占用时自动 +1，与 start.py 的端口策略一致
    proxy: {
      "/api": {
        target: BACKEND_URL,
        changeOrigin: true,
      },
      "/output": {
        target: BACKEND_URL,
        changeOrigin: true,
      },
    },
  },
});
