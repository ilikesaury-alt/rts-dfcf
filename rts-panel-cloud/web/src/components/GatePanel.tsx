/**
 * 飞书过门区（验收 4）。2026-10 UI 重构：
 * - 右栏**唯一**内容（原「本轮计数」三重冗余图已删，见 App.tsx 注释）—— `stats`
 *   七字段收敛成一行紧凑账目（通过/否决/丢弃 三个关键数大号显示，档位细分小号）；
 * - 门外行同样**可点击**进详情（原来只有门内/主表行可点）。
 *
 * ⚠ 标题**不得简化成「飞书推送」**：它回答的是「**下一张卡会推什么**」，
 * 而卡片最多 300s 一次、且只在通过集合变化时才推；终端（此处=面板）每轮都刷新。
 * 故这个区展示的是**将要推的集合**，不是**已经推过的东西**。
 *
 * 两态（切换只换数据源，标题逐字不变）：
 * - 「门内」= `regions.gate.*`（本地 `push_gate.apply_push_gate` 的通过行，与飞书卡片同一份）；
 * - 「门外」= `regions.*` 里**不在**通过集合中的行（按 symbol 做集合差）——
 *   即「被否决 ∨ 分级低于兜底档」的那批，也就是这一轮**不会**被推的票。
 *
 * `stats` 七字段是卡片级判定的账目（总数/通过/A/B/C 档/被否决/无兜底被丢），
 * 一律原样显示 —— 面板不重算、不解释成建议。
 */

import { useState } from "react";

import type { Gate, GateStats, Row } from "../api/types";
import { rowSymbol } from "../lib/cols";
import { stockHref } from "../lib/router";
import { Tabs, TabsList, TabsTrigger } from "./ui/tabs";

/** 卡片标题固定串（改这句要连同 `scanner/view/render.py` 的过门区标题一起看）。 */
const TITLE = "◆ 飞书过门 — 下一张卡会推什么（非已推内容）";

const GROUPS: { key: keyof Omit<Gate, "stats">; title: string }[] = [
  { key: "main", title: "v1 池选" },
  { key: "hist", title: "v1 回捞" },
  { key: "hot", title: "榜内飙升" },
  { key: "offboard", title: "榜外异动" },
];

/** 账目细分（小号行）：主行只显示 通过/否决/丢弃 三个关键数。 */
const STAT_FIELDS: { key: keyof GateStats; label: string }[] = [
  { key: "total", label: "候选总数" },
  { key: "tier_a", label: "A 档" },
  { key: "tier_b", label: "B 档" },
  { key: "tier_c_fallback", label: "C 档兜底" },
  { key: "dropped_no_fallback", label: "无兜底丢弃" },
];

type Props = {
  gate: Gate;
  /** 全量区块行（算「门外」= 全量 − 通过 需要它）。 */
  regions: Record<string, Row[]>;
};

export default function GatePanel({ gate, regions }: Props) {
  const [inside, setInside] = useState(true);

  const rowsFor = (key: keyof Omit<Gate, "stats">): Row[] => {
    if (inside) return gate[key] ?? [];
    const passed = new Set((gate[key] ?? []).map((r) => rowSymbol(r)));
    return (regions[key] ?? []).filter((r) => !passed.has(rowSymbol(r)));
  };

  const open = (symbol: string) => {
    if (symbol) window.location.hash = stockHref(symbol);
  };

  return (
    <aside
      id="anchor-gate"
      className="region-card flex flex-col gap-2"
      aria-label="飞书过门"
    >
      <h2 className="region-title text-[13px]">{TITLE}</h2>

      {/* 关键账目（大号三数）：通过 / 否决 / 无兜底丢弃 —— 卡片级判定的三根支柱 */}
      <div className="grid grid-cols-3 gap-2">
        {(
          [
            { key: "passed" as const, label: "通过" },
            { key: "vetoed" as const, label: "否决" },
            { key: "dropped_no_fallback" as const, label: "丢弃" },
          ]
        ).map((f) => (
          <div key={f.key} className="gate-stat">
            <span className="gate-stat-num tabular-nums">{gate.stats[f.key]}</span>
            <span className="gate-stat-label">{f.label}</span>
          </div>
        ))}
      </div>

      <Tabs value={inside ? "inside" : "outside"} onValueChange={(v) => setInside(v === "inside")}>
        <TabsList className="w-full">
          <TabsTrigger value="inside" className="flex-1">门内（会推）</TabsTrigger>
          <TabsTrigger value="outside" className="flex-1">门外（不推）</TabsTrigger>
        </TabsList>
        {/* 两组内容各自内联渲染在下方（不用 TabsContent）：切页签时标题与
            `gate.stats` 必须**保持不动**（验收 4 / FR-V3），若把列表搬进
            TabsContent 会连带 unmount 计数区，标题随之闪烁。 */}
      </Tabs>

      <dl className="m-0 flex flex-wrap gap-x-3 gap-y-0.5 text-[11px]">
        {STAT_FIELDS.map((f) => (
          <div key={f.key} className="flex items-center gap-1">
            <dt className="m-0 text-text-dim">{f.label}</dt>
            <dd className="m-0 tabular-nums text-text">{gate.stats[f.key]}</dd>
          </div>
        ))}
      </dl>

      {GROUPS.map((g) => {
        const rows = rowsFor(g.key);
        return (
          <section key={g.key} className="flex flex-col gap-0.5">
            <h3 className="m-0 text-[11px] leading-4 font-semibold text-text-dim">
              {g.title} <span className="tabular-nums">{rows.length}</span>
            </h3>
            {rows.length === 0 ? (
              <p className="m-0 text-[12px] leading-7 text-text-dim">—</p>
            ) : (
              <ul className="m-0 max-h-52 list-none overflow-y-auto p-0">
                {rows.map((row, i) => {
                  const sym = rowSymbol(row);
                  const name = typeof row.name === "string" ? row.name : "";
                  return (
                    <li key={`${sym}-${i}`}>
                      <button
                        type="button"
                        onClick={() => open(sym)}
                        className="gate-row"
                        title={name ? `${sym} ${name}` : sym}
                      >
                        <span className="tabular-nums text-text-dim">{sym}</span>
                        <span className="truncate text-text-dim">{name}</span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}
          </section>
        );
      })}
    </aside>
  );
}
