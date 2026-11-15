/**
 * 极简哈希路由（零依赖，design §2.2「不引状态库」）。
 *
 * 面板只有两个页面：五区块列表 与 个股详情。引 `react-router` 为一个 `useState`
 * 能解决的事加一个依赖不划算；用 `location.hash` 还能白拿「刷新/前进后退保持页面」
 * 与「详情页可直接贴给别人」（同一台机器）。
 *
 * ⚠ 生产态 Electron 加载的是 `http://127.0.0.1:8899/`（Python 同源托管 dist），
 *   hash 路由**不需要**服务端 SPA 回退；`local_panel_server` 仍实现了回退，
 *   只是为了将来换成 history 路由时不会 404。
 */

import { useEffect, useState } from "react";

export type Route = { name: "list" } | { name: "stock"; symbol: string };

const STOCK_PREFIX = "#/stock/";

/**
 * 与本地 `local_panel_server._SYMBOL_RE` 同款（6 位代码，可带 0~2 位市场前缀）。
 * 这是**格式**校验，不是业务判断、不是筛选：格式不合法的 hash 直接收敛回列表，
 * 避免「贴了一个无效代码」时停在没有数据的详情空壳（设计 §7 无效票收敛到稳定视图）。
 */
const SYMBOL_RE = /^[A-Za-z]{0,2}\d{6}$/;

/** 解析 `location.hash` → 路由（未知 / 格式非法 hash 一律回列表，不抛错）。 */
export function parseHash(hash: string): Route {
  if (hash.startsWith(STOCK_PREFIX)) {
    let symbol = "";
    try {
      symbol = decodeURIComponent(hash.slice(STOCK_PREFIX.length)).trim();
    } catch {
      // 手工贴进来的坏转义（如 `%ZZ`）不该让整页崩：回列表。
      return { name: "list" };
    }
    if (SYMBOL_RE.test(symbol)) return { name: "stock", symbol };
  }
  return { name: "list" };
}

/** 详情页链接（组件里拼链接一律走它，避免前缀散落各处）。 */
export function stockHref(symbol: string): string {
  return `${STOCK_PREFIX}${encodeURIComponent(symbol)}`;
}

export const listHref = "#/list";

/**
 * 「来源行」记忆（v2 详情返回焦点归还）：整行/门内按钮进详情时记下 DOM id，
 * 回到列表后把焦点还给同一个元素。纯视图状态，不进载荷、不落库、刷新不要求保留。
 */
let openerId = "";

/** 进详情：记下来源元素 id 再切 hash（不新增请求，仍是 `hashchange` 驱动 `useRoute`）。 */
export function openStock(symbol: string, fromId?: string): void {
  if (fromId) openerId = fromId;
  const next = stockHref(symbol);
  if (window.location.hash !== next) window.location.hash = next;
}

/** 返回列表（返回按钮 / Esc 共用）。 */
export function goToList(): void {
  if (window.location.hash !== listHref) window.location.hash = listHref;
}

/** 取出并清空来源 id（只在「切回列表」时调一次，避免下次误还焦点）。 */
export function consumeOpener(): string {
  const id = openerId;
  openerId = "";
  return id;
}

/** 订阅 hash 变化的路由钩子（`hashchange` 是唯一真源，无内部状态可漂移）。 */
export function useRoute(): Route {
  const [route, setRoute] = useState<Route>(() => parseHash(window.location.hash));
  useEffect(() => {
    const normalize = (): Route => {
      const next = parseHash(window.location.hash);
      // 非法/未知 hash 统一收敛到 `#/list`（replaceState 不污染前进/后退历史）。
      // 这样刷新与浏览器前进/后退看到的 URL 和视图始终一致。
      if (next.name === "list" && window.location.hash !== listHref) {
        window.history.replaceState(null, "", listHref);
      }
      return next;
    };
    // 首次挂载也要收敛（直接贴进来的坏 hash 不会触发 hashchange）：
    // state 初始值已经按 parseHash 算好，只需把地址栏归一化，不必再 setRoute。
    normalize();
    const onChange = () => setRoute(normalize());
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return route;
}
