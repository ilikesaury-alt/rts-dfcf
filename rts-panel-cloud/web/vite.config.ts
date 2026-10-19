import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

/**
 * 前端工程配置（design §2.3 §7.1）。
 *
 * **不引状态库、不引 SSR**（design §2.2「不拆」理由）：面板状态只有
 * 「当前快照 + 滞后 + 路由」三项，`useState` + `useEffect` 足够；
 * 纯静态导出即可，不需要 SSR 框架。
 */
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true, // 端口被占就报错，不静默跳 5174（否则代理目标对不上，易误判）
    // 同源代理：开发期前端 fetch('/api/...') 由 Vite 转发到**本机扫描器**的
    // 本地只读服务（`scanner/local_panel_server.py`，默认 127.0.0.1:8899）。
    // 于是开发期也不存在跨域，与生产态（Electron 直接加载 `http://127.0.0.1:8899/`，
    // 由 Python 同源托管 dist）拓扑一致 —— 两端 `/api` 都是相对路径。
    // ⚠ 端口改了要同步 `scanner.config.LOCAL_PANEL_PORT` 与 Electron 的注入值。
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8899",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
});
