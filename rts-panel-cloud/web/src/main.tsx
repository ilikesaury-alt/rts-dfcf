import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import App from "./App";
import "./styles.css";

/**
 * 前端入口（T1.1 / design §2.3）。
 *
 * `StrictMode` 保留：React 19 的 StrictMode 会双调用 render，恰好能提前暴露
 * 「在 render 里发请求 / 改外部状态」这类问题 —— 本工程把网络收口在
 * `api/client.ts`（T1.2），正是要杜绝这种写法。
 */
const el = document.getElementById("root");
if (!el) throw new Error("#root not found");

createRoot(el).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
