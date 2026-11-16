/**
 * 个股详情页（v2 §5.2）：沿用同一外壳（标题栏 / 读数带 / 页脚），隐藏侧栏与右栏。
 *
 * 三点约定（不变）：
 * 1. **数据全部来自 `/api/stock/{symbol}/kline`**（本地服务现算 `daily_kline` +
 *    `recommendations`）。列表页不预先抓 K 线（D4），所以这一页是唯一会碰 K 线的视图。
 * 2. **K 线元组顺序是 `[date, open, close, low, high, volume]`** —— `close` 在
 *    `low/high` 之前（ECharts 约定）。映射到 `KLineData` 时必须逐位对齐。
 * 3. **分解只显示、不重算**：面板把 `score_breakdown` 的数值项列出并给出合计，
 *    不解释成结论（G5 零第二结论源）。
 *
 * v2 交互：返回按钮 + Esc 返回 `#/list`（`lib/router.ts`），返回后焦点还给来源行；
 * 进详情后返回按钮自动获得焦点。K 线仍是真实数据（本页不做「无行情数值」占位 ——
 * 那是静态预览稿的示意，真实页面必须呈现服务端下发的真实 K 线）。
 */

import { useEffect, useMemo, useRef, useState } from "react";

import { KLineChart, lightTheme, type KLineData, type KLineTheme } from "kline-component";

import { API, ApiError, apiGet } from "../api/client";
import type { ExplainResponse, KlineResponse } from "../api/types";
import { fmtPct, num } from "../lib/cols";
import { goToList } from "../lib/router";
import EmptyState from "../components/EmptyState";

/**
 * A 股口径**浅色**主题（2026-11-16 v3 主题重做：面板整体转浅底，K 线跟随）。
 *
 * 库自带的 `lightTheme` 已给出白底 + 浅网格，这里只翻两样：
 * 1. **涨跌色**改成 A 股口径（红涨绿跌，库默认是欧美口径绿涨红跌）；
 * 2. 网格/十字线压到本项目 `--line` 同一档，避免和卡片边框抢眼。
 *
 * ⚠ 与 `index.css` 的 `--color-up/down` 持有同一组字面值 —— 改色必须两处同步
 *   （K 线是 Canvas，读不到 CSS 变量，这是既有约定，不是疏漏）。
 */
const ASHARE_THEME: KLineTheme = {
  ...lightTheme,
  upColor: "#d9222a",
  downColor: "#0a8f3c",
  upWickColor: "#d9222a",
  downWickColor: "#0a8f3c",
  macdBarUpColor: "#d9222a",
  macdBarDownColor: "#0a8f3c",
  gridColor: "rgba(15, 23, 42, 0.06)",
  crosshairColor: "rgba(15, 23, 42, 0.22)",
  crosshairTextBg: "rgba(255, 255, 255, 0.96)",
  crosshairTextColor: "#0f1b2d",
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

/** 分解项的展示文本（**异质 dict**：既有数值也有字符串，原样列出）。 */
function breakdownText(v: unknown): string {
  if (typeof v === "number") return Number.isFinite(v) ? v.toFixed(2) : "—";
  if (typeof v === "string") return v;
  return "—"; // null / 对象 / 数组：契约里不该出现，显 — 不显 "null"
}

/**
 * 分解项的数值视图：`items` 原样列出（含非数值项），`total` **只累加数值项**。
 * ⚠ 合计是「各分项之和」，**不是**评分：面板不重算、不解释（G5）。
 */
function breakdownItems(breakdown: Record<string, number> | null | undefined) {
  const entries = Object.entries(breakdown ?? {});
  const numeric = entries.filter(([, v]) => typeof v === "number" && Number.isFinite(v));
  const total = numeric.length > 0 ? numeric.reduce((acc, [, v]) => acc + (v as number), 0) : null;
  return { items: entries.map(([key, v]) => ({ key, text: breakdownText(v) })), total };
}

type Props = { symbol: string; name?: string };

export default function Stock({ symbol, name }: Props) {
  const [data, setData] = useState<KlineResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const backRef = useRef<HTMLButtonElement | null>(null);

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

  // 进详情后把焦点交给返回按钮（Esc 与点击都从这里回到列表）。
  useEffect(() => {
    backRef.current?.focus({ preventScroll: true });
  }, [symbol]);

  const klineData = useMemo(() => toKLineData(data?.kline ?? []), [data]);
  const { items, total } = useMemo(() => breakdownItems(data?.scoreBreakdown), [data]);

  return (
    <main className="detail-main" data-scroll-root="main">
      <div className="detail-wrap">
        <div className="detail-toolbar">
          <button type="button" className="back-btn" ref={backRef} onClick={goToList}>
            <span className="arrow" aria-hidden="true">←</span>返回列表
          </button>
          <span className="detail-note">按 Esc 也可返回 · 列表位置保持切换前状态</span>
        </div>

        <div className="detail-left">
          <section className="detail-card">
            <div className="detail-card-head">
              <span className="card-accent" style={{ background: "var(--brand)" }} aria-hidden="true" />
              <div className="card-title-wrap">
                <h2>
                  <span className="mono">{symbol}</span>
                  {name ? ` · ${name}` : ""}
                </h2>
                <p>
                  日线 · 主图 / 副图 · 口径同本机扫描器最后一轮快照
                  {data ? ` · K 线截至 ${data.klineDate}（日线 ${data.kline.length} 根）` : ""}
                </p>
              </div>
            </div>
            <div className="detail-card-body">
              {loading ? <p className="state-line">加载中…</p> : null}
              {error ? <p className="state-line is-error">{errorText(error)}</p> : null}
              {data ? (
                klineData.length > 0 ? (
                  <div className="chart-frame" style={{ height: CHART_HEIGHT }}>
                    <KLineChart data={klineData} theme={ASHARE_THEME} indicators={["MA", "VOL", "MACD"]} />
                  </div>
                ) : (
                  <EmptyState note="该票在本机日线数据中没有可绘制的 K 线；口径与上方区块独立。" />
                )
              ) : null}
            </div>
          </section>
        </div>

        <div className="detail-right">
          <section className="detail-card">
            <div className="detail-card-head">
              <span className="card-accent" aria-hidden="true" />
              <div className="card-title-wrap">
                <h2>本轮字段</h2>
                <p>该股在最后一轮快照中的分解项 · 只读呈现</p>
              </div>
            </div>
            {data && items.length > 0 ? (
              <table className="mini-table">
                <tbody>
                  {items.map((item) => (
                    <tr key={item.key}>
                      <th className="font-normal text-text-dim">{item.key}</th>
                      <td className="r">{item.text}</td>
                    </tr>
                  ))}
                  <tr className="kv-total">
                    <th>合计</th>
                    <td className="r">{total === null ? "—" : total.toFixed(2)}</td>
                  </tr>
                </tbody>
              </table>
            ) : (
              <div className="detail-card-body">
                <EmptyState note="该股在本轮快照里没有分解项；不补写历史记录。" />
              </div>
            )}
            {data && items.length > 0 ? (
              <p className="gate-foot">合计为各分项之和；面板不做评分判断。</p>
            ) : null}
          </section>

          <section className="detail-card">
            <div className="detail-card-head">
              <span className="card-accent" aria-hidden="true" />
              <div className="card-title-wrap">
                <h2>出现史</h2>
                <p>本轮快照中的出现记录 · 只读呈现</p>
              </div>
            </div>
            {data && data.appearances.length > 0 ? (
              <div className="max-w-full overflow-x-auto">
                <table className="mini-table">
                  <thead>
                    <tr>
                      <th>日期</th>
                      <th>时间</th>
                      <th>类别</th>
                      <th className="r">评分</th>
                      <th className="r">涨幅</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.appearances.map((row, i) => {
                      const pct = num(row.percent);
                      return (
                        <tr key={`${String(row.date)}-${String(row.time)}-${i}`}>
                          <td>{String(row.date ?? "")}</td>
                          <td>{String(row.time ?? "")}</td>
                          <td>{String(row.category ?? "")}</td>
                          <td className="r">{num(row.score)?.toFixed(0) ?? "—"}</td>
                          <td className={`r${pct === null ? "" : pct > 0 ? " tone-up" : " tone-down"}`}>
                            {fmtPct(pct)}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            ) : (
              <div className="detail-card-body">
                <EmptyState note="该股在本轮快照里没有出现记录；不补写历史。" />
              </div>
            )}
          </section>

          <AiExplain symbol={symbol} />
        </div>
      </div>
    </main>
  );
}

function errorText(code: string): string {
  if (code === "NOT_IN_REPORT") return "本轮上报未包含该票";
  if (code === "BAD_REQUEST") return "代码格式不对（应为 6 位代码或带 SH/SZ/BJ 前缀）";
  if (code === "AI_UNAVAILABLE") return "AI 解读暂不可用：未配置模型服务凭据";
  if (code === "AI_UPSTREAM") return "AI 解读服务暂时不可用，请稍后重试";
  if (code === "AI_RATE_LIMITED") return "该标的刚解读过，请稍后再试";
  if (code === "NETWORK") return "本地服务无响应";
  return `读取失败（${code}）`;
}

/**
 * AI 解读区块（E3，ADR 0001）。
 * 1. **点击才请求**（P4）：进页不自动打模型，调用量 = 主动点击数。
 * 2. `label` 只渲染服务端下发的 `data.label`，**本文件不写那句标签文案** ——
 *    红线标签必须单源在服务端（守卫 `test_ai_label_not_hardcoded_in_web`）。
 * 3. `text` 原样展示，不重写、不摘要；`redlined=true` 时用错误色提醒「已被拦下」。
 */
function AiExplain({ symbol }: { symbol: string }) {
  const [data, setData] = useState<ExplainResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [showMaterials, setShowMaterials] = useState(false);

  const run = () => {
    setLoading(true);
    setError(null);
    apiGet<ExplainResponse>(API.aiExplain(symbol))
      .then((res) => setData(res))
      .catch((e: unknown) => setError(e instanceof ApiError ? e.code : "INTERNAL"))
      .finally(() => setLoading(false));
  };

  return (
    <section className="detail-card">
      <div className="detail-card-head">
        <span className="card-accent" aria-hidden="true" />
        <div className="card-title-wrap">
          <h2>AI 解读</h2>
          <p>点击才请求 · 素材逐项可核对</p>
        </div>
        <button type="button" className="ai-btn" onClick={run} disabled={loading}>
          {loading ? "生成中…" : data ? "重新解读" : "生成解读"}
        </button>
      </div>
      <div className="detail-card-body flex flex-col gap-2">
        <p className="state-line">解读由模型转述本轮已上报的数据生成，素材逐项列在下方可核对；系统不由此产生任何新的评分或判断。</p>
        {error ? <p className="state-line is-error">{errorText(error)}</p> : null}
        {data ? (
          <>
            <p className={`state-line font-semibold ${data.redlined ? "is-error" : ""}`}>{data.label}</p>
            <p className={`ai-text ${data.redlined ? "is-error" : ""}`}>{data.text}</p>
            {data.missing.length > 0 ? (
              <p className="state-line">缺失来源：{data.missing.join("、")}（模型未收到这些数据，不得补）</p>
            ) : null}
            <div className="flex flex-wrap items-center gap-2">
              <button type="button" className="ai-btn" onClick={() => setShowMaterials((v) => !v)}>
                {showMaterials ? "收起素材" : `查看素材（${data.materials.length} 项）`}
              </button>
              {data.generatedFrom ? (
                <span className="detail-note">
                  依据快照 {data.generatedFrom.date} {data.generatedFrom.time}
                  {data.generatedFrom.klineDate ? ` · K 线截至 ${data.generatedFrom.klineDate}` : ""}
                </span>
              ) : (
                <span className="detail-note">依据已上报的 K 线缓存</span>
              )}
              {data.cached ? <span className="detail-note">· 缓存命中</span> : null}
            </div>
            {showMaterials ? (
              <div className="max-w-full overflow-x-auto">
                <table className="mini-table">
                  <tbody>
                    {data.materials.map((m) => (
                      <tr key={m.id}>
                        <th className="font-normal whitespace-nowrap text-text-dim">{m.label}</th>
                        <td className="break-all">{m.value}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : null}
          </>
        ) : null}
      </div>
    </section>
  );
}
