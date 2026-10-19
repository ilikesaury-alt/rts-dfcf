/**
 * 个股详情页（验收 5）：K 线 + 成交量（`kline-component`，纯 Canvas）+ 评分分解 + 出现史。
 *
 * 三点约定：
 *
 * 1. **数据全部来自 `/api/stock/{symbol}/kline`**（本地服务现算 `daily_kline` +
 *    `recommendations`）。列表页不预先抓 K 线（D4），所以这一页是唯一会碰 K 线的视图。
 * 2. **K 线元组顺序是 `[date, open, close, low, high, volume]`** —— `close` 在
 *    `low/high` 之前（ECharts 约定，见 `api/types.ts` 的 `Stock.kline`）。映射到
 *    `kline-component` 的 `KLineData` 时必须逐位对齐，看错位不会报错，只会画出一根
 *    上下影线互换的蜡烛。
 * 3. **评分分解只显示、不重算**：面板把 `score_breakdown` 的数值项列出并给出合计，
 *    不解释成评分结论（G5 零第二结论源）。非 v1 票没有分解 → 显「无评分分解」。
 */

import { useEffect, useMemo, useState } from "react";

import { KLineChart, darkTheme, type KLineData, type KLineTheme } from "kline-component";

import { API, ApiError, apiGet } from "../api/client";
import type { KlineResponse } from "../api/types";
import { fmtPct, num } from "../lib/cols";
import { listHref } from "../lib/router";

/**
 * A 股口径暗色主题：组件自带暗色底，只翻涨跌色（**红涨绿跌**，与面板其余部分一致）。
 * 不做整套自绘主题 —— 换配色只该换这几个变量，散落的自定义色会与 `styles.css` 分叉。
 */
const ASHARE_THEME: KLineTheme = {
  ...darkTheme,
  upColor: "#ef232a",
  downColor: "#14b143",
  upWickColor: "#ef232a",
  downWickColor: "#14b143",
  macdBarUpColor: "#ef232a",
  macdBarDownColor: "#14b143",
};

/** K 线区高度（px）。固定像素：Canvas 需要一个确定的高度才不会画在 0 高画布上。 */
const CHART_HEIGHT = 420;

function toKLineData(kline: number[][]): KLineData[] {
  return kline.map((row) => {
    const [date, open, close, low, high, volume] = row;
    return {
      timestamp: new Date(`${date}T15:00:00+08:00`).getTime(),
      open,
      close,
      low,
      high,
      volume,
    };
  });
}

/** 分解项的展示文本（**异质 dict**：既有数值也有字符串，`stock_report.py` 同款原样列出）。 */
function breakdownText(v: unknown): string {
  if (typeof v === "number") return Number.isFinite(v) ? v.toFixed(2) : "—";
  if (typeof v === "string") return v;
  return "—"; // null / 对象 / 数组：契约里不该出现，显 — 不显 "null"
}

/**
 * 分解项的数值视图：`items` 原样列出（含非数值项），`total` **只累加数值项**。
 *
 * ⚠ 合计是「各分项之和」，**不是**评分：`score_breakdown` 是诊断用的异质 dict
 * （含 `concept` 这类字符串），面板不重算、不解释（G5）。
 */
function breakdownItems(breakdown: Record<string, number> | null | undefined) {
  const entries = Object.entries(breakdown ?? {});
  const numeric = entries.filter(([, v]) => typeof v === "number" && Number.isFinite(v));
  const total = numeric.length > 0 ? numeric.reduce((acc, [, v]) => acc + (v as number), 0) : null;
  return { items: entries.map(([key, v]) => ({ key, text: breakdownText(v) })), total };
}

type Props = { symbol: string };

export default function Stock({ symbol }: Props) {
  const [data, setData] = useState<KlineResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setData(null);
    apiGet<KlineResponse>(API.stockKline(symbol))
      .then((res) => {
        if (!cancelled) setData(res);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof ApiError ? e.code : "INTERNAL");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [symbol]);

  const klineData = useMemo(() => toKLineData(data?.kline ?? []), [data]);
  const { items, total } = useMemo(() => breakdownItems(data?.scoreBreakdown), [data]);

  return (
    <div className="stock">
      <div className="stock-head">
        <a className="back" href={listHref}>
          ← 返回列表
        </a>
        <h1 className="stock-title">
          {symbol}
          <span className="stock-sub">
            {data ? ` · K 线截至 ${data.klineDate}（日线 ${data.kline.length} 根）` : ""}
          </span>
        </h1>
      </div>

      {loading ? <p className="stock-note">加载中…</p> : null}
      {error ? <p className="stock-note stock-error">{errorText(error)}</p> : null}

      {data ? (
        <>
          <section className="stock-section">
            {klineData.length > 0 ? (
              <div className="kline-box" style={{ height: CHART_HEIGHT }}>
                <KLineChart data={klineData} theme={ASHARE_THEME} indicators={["MA", "VOL", "MACD"]} />
              </div>
            ) : (
              <p className="stock-note">该票无本地日线数据</p>
            )}
          </section>

          <section className="stock-section">
            <h2>评分分解</h2>
            {items.length === 0 ? (
              <p className="stock-note">无评分分解</p>
            ) : (
              <table className="kv">
                <tbody>
                  {items.map((item) => (
                    <tr key={item.key}>
                      <th>{item.key}</th>
                      <td className="cell-r">{item.text}</td>
                    </tr>
                  ))}
                  <tr className="kv-total">
                    <th>合计</th>
                    <td className="cell-r">{total === null ? "—" : total.toFixed(2)}</td>
                  </tr>
                </tbody>
              </table>
            )}
            {items.length > 0 ? <p className="stock-hint">合计为各分项之和；面板不做评分判断。</p> : null}
          </section>

          <section className="stock-section">
            <h2>出现史（最近 {data.appearances.length} 条）</h2>
            {data.appearances.length === 0 ? (
              <p className="stock-note">无出现记录</p>
            ) : (
              <div className="table-scroll">
                <table className="region-table">
                  <thead>
                    <tr>
                      <th className="cell-l">日期</th>
                      <th className="cell-l">时间</th>
                      <th className="cell-l">类别</th>
                      <th className="cell-r">评分</th>
                      <th className="cell-r">涨幅</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.appearances.map((row, i) => {
                      const pct = num(row.percent);
                      return (
                        <tr key={`${String(row.date)}-${String(row.time)}-${i}`}>
                          <td className="cell-l">{String(row.date ?? "")}</td>
                          <td className="cell-l">{String(row.time ?? "")}</td>
                          <td className="cell-l">{String(row.category ?? "")}</td>
                          <td className="cell-r">{num(row.score)?.toFixed(0) ?? "—"}</td>
                          <td className={`cell-r${pct === null ? "" : pct > 0 ? " tone-up" : " tone-down"}`}>{fmtPct(pct)}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </>
      ) : null}
    </div>
  );
}

function errorText(code: string): string {
  if (code === "NOT_IN_REPORT") return "本轮上报未包含该票";
  if (code === "NETWORK") return "本地服务无响应";
  if (code === "BAD_REQUEST") return "股票代码不合法";
  return `读取失败（${code}）`;
}
