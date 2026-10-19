/**
 * 飞书过门区（验收 4）。
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

/** 卡片标题固定串（改这句要连同 `scanner/view/render.py` 的过门区标题一起看）。 */
const TITLE = "◆ 飞书过门 — 下一张卡会推什么（非已推内容）";

const GROUPS: { key: keyof Omit<Gate, "stats">; title: string }[] = [
  { key: "main", title: "v1 池选" },
  { key: "hist", title: "v1 回捞" },
  { key: "hot", title: "榜内飙升" },
  { key: "offboard", title: "榜外异动" },
];

const STAT_FIELDS: { key: keyof GateStats; label: string }[] = [
  { key: "total", label: "候选总数" },
  { key: "passed", label: "通过" },
  { key: "tier_a", label: "A 档" },
  { key: "tier_b", label: "B 档" },
  { key: "tier_c_fallback", label: "C 档兜底" },
  { key: "vetoed", label: "否决" },
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

  return (
    <aside className="gatepanel" aria-label="飞书过门">
      <h2 className="gatepanel-title">{TITLE}</h2>
      <div className="gatepanel-toggle" role="group" aria-label="门内门外">
        <button type="button" className={inside ? "toggle on" : "toggle"} aria-pressed={inside} onClick={() => setInside(true)}>
          门内（会推）
        </button>
        <button type="button" className={inside ? "toggle" : "toggle on"} aria-pressed={!inside} onClick={() => setInside(false)}>
          门外（不推）
        </button>
      </div>

      <dl className="gate-stats">
        {STAT_FIELDS.map((f) => (
          <div className="gate-stat" key={f.key}>
            <dt>{f.label}</dt>
            <dd>{gate.stats[f.key]}</dd>
          </div>
        ))}
      </dl>

      {GROUPS.map((g) => {
        const rows = rowsFor(g.key);
        return (
          <section className="gate-group" key={g.key}>
            <h3>
              {g.title} <span className="gate-count">{rows.length}</span>
            </h3>
            {rows.length === 0 ? (
              <p className="gate-empty">—</p>
            ) : (
              <ul className="gate-list">
                {rows.map((row, i) => (
                  <li key={`${rowSymbol(row)}-${i}`}>
                    <span className="gate-symbol">{rowSymbol(row)}</span>
                    <span className="gate-name">{typeof row.name === "string" ? row.name : ""}</span>
                  </li>
                ))}
              </ul>
            )}
          </section>
        );
      })}
    </aside>
  );
}
