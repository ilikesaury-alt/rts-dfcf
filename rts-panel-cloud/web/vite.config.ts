import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

/**
 * 前端工程配置（T1.1 / design §2.3 §7.1）。
 *
 * **不引状态库、不引 SSR**（design §2.2「不拆」理由）：面板状态只有
 * 「当前快照 + 滞后 + 路由」三项，`useState` + Context 足够；
 * 纯静态导出即可，不需要 SSR 框架。
 */
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true, // 端口被占就报错，不静默跳 5174（否则代理目标对不上，易误判）
    // 同源代理：开发期前端 fetch('/api/...') 由 Vite 转发到本地 Worker，
    // 于是**开发期也不存在跨域**，与生产「同域 routes」拓扑保持一致
    // （design §3.2 第 6 层 / T2.2 复核修 7）。
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8787",
        changeOrigin: true,
        // SSE 用（M3）：禁掉缓冲，否则 event 流会被 vite 攒住不下发。
        configure: (proxy) => {
          proxy.on("proxyRes", (proxyRes) => {
            if (proxyRes.headers["content-type"]?.includes("text/event-stream")) {
              proxyRes.headers["cache-control"] = "no-cache";
            }
          });
        },
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
});
