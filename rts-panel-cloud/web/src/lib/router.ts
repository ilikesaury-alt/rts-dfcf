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

/** 解析 `location.hash` → 路由（未知 hash 一律回列表，不抛错）。 */
export function parseHash(hash: string): Route {
  if (hash.startsWith(STOCK_PREFIX)) {
    const symbol = decodeURIComponent(hash.slice(STOCK_PREFIX.length)).trim();
    if (symbol) return { name: "stock", symbol };
  }
  return { name: "list" };
}

/** 详情页链接（组件里拼链接一律走它，避免前缀散落各处）。 */
export function stockHref(symbol: string): string {
  return `${STOCK_PREFIX}${encodeURIComponent(symbol)}`;
}

export const listHref = "#/";

/** 订阅 hash 变化的路由钩子（`hashchange` 是唯一真源，无内部状态可漂移）。 */
export function useRoute(): Route {
  const [route, setRoute] = useState<Route>(() => parseHash(window.location.hash));
  useEffect(() => {
    const onChange = () => setRoute(parseHash(window.location.hash));
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return route;
}
