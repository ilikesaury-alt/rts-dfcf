/**
 * 只读统计图（2026-10 桌面 UI 现代化 · Slice 4 / CONTEXT.md Q3）。
 *
 * ⚠ 这是本项目**唯一**新增的可视化，边界写死：
 *
 * 1. **只消费已随 `/api/regions` 下发的字段** —— `regions.gate.stats`（7 个计数）
 *    与 `regions.*` 的行集合。**零新增 fetch**（网络收口纪律：`client.ts` 之外
 *    不得出现 `fetch`，`test_panel_web_hygiene.py` 守着）。
 * 2. **不产生第二结论源**（G5）：图只回答「这一轮各组**有多少只**」这个事实，
 *    标题与坐标一律是**计数/分布**这类可数事实。**不得**出现「更值得买」
 *    「胜率」「建议」之类评价语 —— `test_panel_web_cols.py` 第 4 条 grep 兜底。
 * 3. **不重算**：`gate.stats` 是本地 `push_gate.apply_push_gate` 已算好的账目
 *    （design §3.6），前端只搬运，不按阈值重新分组。
 *
 * 为什么用 Recharts 而不是继续 `kline-component`：那是 K 线专用 Canvas 组件，
 * 这里要的是「横向条形 + 分段占比」这类普通统计图，Recharts 更合适；两者都是
 * 纯前端渲染，都不新增数据通路。
 */

import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip as RTooltip } from "recharts";

import type { GateStats } from "@/api/types";
import { cn } from "@/lib/utils";

/** 过门账目七字段的**展示名与顺序**（值原样搬运，不重算、不解释）。 */
const STAT_ROWS: { key: keyof GateStats; label: string }[] = [
  { key: "total", label: "候选总数" },
  { key: "passed", label: "通过" },
  { key: "tier_a", label: "A 档" },
  { key: "tier_b", label: "B 档" },
  { key: "tier_c_fallback", label: "C 档兜底" },
  { key: "vetoed", label: "否决" },
  { key: "dropped_no_fallback", label: "无兜底丢弃" },
];

/** 分段占比用的三个色（与面板暗底同源，非语义色 —— 只区分扇区，不表好坏）。 */
const PIE_COLORS = ["#3b82f6", "#22c55e", "#f5a623"];

type Props = {
  stats: GateStats;
  /** 四个区块的行数（计数用，来自 `regions.*` 本身）。 */
  counts: { main: number; hist: number; hot: number; offboard: number };
};

/**
 * 过门账目：**横向条形**（通过 vs 否决 vs 丢弃）。
 *
 * 为什么不用饼图做主图：三类之和恒等于 `total`，饼图表达的是同一件事却更难读数；
 * 条形可以并排放「档位细分」，一眼看出通过量分布在哪个档。
 */
function GateBreakdown({ stats }: { stats: GateStats }) {
  const bars = [
    { name: "A 档", value: stats.tier_a, fill: PIE_COLORS[0] },
    { name: "B 档", value: stats.tier_b, fill: PIE_COLORS[1] },
    { name: "C 档兜底", value: stats.tier_c_fallback, fill: PIE_COLORS[2] },
  ];

  return (
    <div className="flex flex-col gap-1">
      {bars.map((b) => {
        const pct = stats.passed > 0 ? (b.value / stats.passed) * 100 : 0;
        return (
          <div key={b.name} className="flex items-center gap-2 text-[12px]">
            <span className="w-14 shrink-0 text-text-dim">{b.name}</span>
            <div className="h-2 flex-1 overflow-hidden rounded-full bg-white/6">
              <div className="h-full rounded-full" style={{ width: `${pct}%`, background: b.fill }} />
            </div>
            <span className="w-8 shrink-0 text-right tabular-nums text-text-dim">{b.value}</span>
          </div>
        );
      })}
    </div>
  );
}

/** 四区块行数占比（**计数**，非好坏；文案只陈述「各有多少只」）。 */
function RegionCounts({ counts }: { counts: Props["counts"] }) {
  const data = [
    { name: "v1 池选", value: counts.main, fill: PIE_COLORS[0] },
    { name: "v1 回捞", value: counts.hist, fill: PIE_COLORS[1] },
    { name: "榜内飙升", value: counts.hot, fill: PIE_COLORS[2] },
    { name: "榜外异动", value: counts.offboard },
  ].filter((d) => d.value > 0);

  if (data.length === 0) {
    return <p className="text-[12px] leading-7 text-text-dim">—（本轮无数据）</p>;
  }

  return (
    <div className="flex items-center gap-2">
      <div className="size-20 shrink-0">
        <ResponsiveContainer width="100%" height="100%">
          <PieChart>
            <Pie data={data} dataKey="value" nameKey="name" innerRadius="60%" outerRadius="100%" strokeWidth={1}>
              {data.map((d) => (
                <Cell key={d.name} fill={d.fill} />
              ))}
            </Pie>
            <RTooltip
              contentStyle={{
                background: "#171a21",
                border: "1px solid #262b36",
                borderRadius: 6,
                fontSize: 12,
                color: "#e6e8ee",
              }}
              formatter={(value, name) => [`${Number(value ?? 0)} 只`, String(name)]}
            />
          </PieChart>
        </ResponsiveContainer>
      </div>
      <ul className="flex flex-col gap-0.5 text-[12px]">
        {data.map((d) => (
          <li key={d.name} className="flex items-center gap-1.5">
            <span className="size-2 shrink-0 rounded-sm" style={{ background: d.fill }} />
            <span className="text-text-dim">{d.name}</span>
            <span className="tabular-nums text-text">{d.value}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export default function StatsCharts({ stats, counts }: Props) {
  return (
    <section className={cn("flex flex-col gap-2 rounded-lg border border-border bg-card px-3 py-2 shadow-sm")} aria-label="本轮计数">
      <h2 className="text-[13px] leading-5 font-semibold text-text">本轮计数</h2>

      <div className="flex flex-col gap-1">
        <h3 className="text-[11px] leading-4 text-text-dim">通过门的档位分布</h3>
        <GateBreakdown stats={stats} />
      </div>

      <div className="flex flex-col gap-1">
        <h3 className="text-[11px] leading-4 text-text-dim">各区块只数</h3>
        <RegionCounts counts={counts} />
      </div>

      <dl className="grid grid-cols-2 gap-x-2 gap-y-0.5 text-[12px]">
        {STAT_ROWS.map((r) => (
          <div key={r.key} className="flex items-center justify-between gap-2">
            <dt className="text-text-dim">{r.label}</dt>
            <dd className="m-0 tabular-nums text-text">{stats[r.key]}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}
