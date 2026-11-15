/**
 * 飞书过门区（v2 §5.5）：右栏唯一内容；账目四段重组 + 带滑动指示条的分段控件。
 *
 * ⚠ 标题**不得简化成「飞书推送」**：它回答的是「**下一张卡会推什么**」，
 * 而卡片最多 300s 一次、且只在通过集合变化时才推；终端（此处=面板）每轮都刷新。
 * 故这个区展示的是**将要推的集合**，不是**已经推过的东西**。
 *
 * 两态（切换只换数据源，标题与 `gate.stats` 账目**不重排、不 unmount**）：
 * - 「门内」= `regions.gate.*`（本地 `push_gate.apply_push_gate` 的通过行）；
 * - 「门外」= `regions.*` 里**不在**通过集合中的行（按 symbol 做集合差）。
 *
 * `stats` 七字段是卡片级判定的账目，一律原样显示 —— 面板不重算、不解释成结论。
 * 分段控件 `role="tablist"` + `aria-selected` + 单向 Tab 焦点，← → / Home / End 切换，
 * 滑块 150ms；清单行可点进详情，hover / active / focus 三态与表格行同一套反馈。
 */

import { useRef, type KeyboardEvent } from "react";

import type { Gate, GateStats, Row } from "../api/types";
import { rowSymbol } from "../lib/cols";
import { openStock } from "../lib/router";

/** 卡片标题固定串（改这句要连同 `scanner/view/render.py` 的过门区标题一起看）。 */
const TITLE = "◆ 飞书过门 — 下一张卡会推什么（非已推内容）";

const GROUPS: { key: keyof Omit<Gate, "stats">; title: string }[] = [
  { key: "main", title: "v1 池选" },
  { key: "hist", title: "v1 回捞" },
  { key: "hot", title: "榜内飙升" },
  { key: "offboard", title: "榜外异动" },
];

/** 账目主行三数（22px 读数）+ 细分带四数：七字段一个不少、一个不增。 */
const PRIMARY: { key: keyof GateStats; label: string; brand?: boolean }[] = [
  { key: "total", label: "候选总数" },
  { key: "passed", label: "通过", brand: true },
  { key: "dropped_no_fallback", label: "无兜底丢弃" },
];

const SECONDARY: { key: keyof GateStats; label: string }[] = [
  { key: "tier_a", label: "A 档" },
  { key: "tier_b", label: "B 档" },
  { key: "tier_c_fallback", label: "C 档兜底" },
  { key: "vetoed", label: "否决" },
];

type Props = {
  gate: Gate;
  /** 全量区块行（算「门外」= 全量 − 通过 需要它）。 */
  regions: Record<string, Row[]>;
  /** 当前分段（由 App 持有并写 localStorage）。 */
  tab: "inside" | "outside";
  onTabChange: (tab: "inside" | "outside") => void;
  /** 卡片是否处于当前导航高亮区。 */
  active?: boolean;
  collapsed?: boolean;
  onToggleCollapse?: () => void;
};

export default function GatePanel({
  gate,
  regions,
  tab,
  onTabChange,
  active = false,
  collapsed = false,
  onToggleCollapse,
}: Props) {
  const inside = tab === "inside";
  const tabRefs = useRef<Record<string, HTMLButtonElement | null>>({});

  const rowsFor = (key: keyof Omit<Gate, "stats">): Row[] => {
    if (inside) return gate[key] ?? [];
    const passed = new Set((gate[key] ?? []).map((r) => rowSymbol(r)));
    return (regions[key] ?? []).filter((r) => !passed.has(rowSymbol(r)));
  };

  const open = (symbol: string) => {
    if (symbol) openStock(symbol, `gate-${symbol}`);
  };

  const onTabKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    let next: "inside" | "outside" | null = null;
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      next = inside ? "outside" : "inside";
    } else if (e.key === "Home") {
      next = "inside";
    } else if (e.key === "End") {
      next = "outside";
    }
    if (next) {
      e.preventDefault();
      onTabChange(next);
      tabRefs.current[next]?.focus();
    }
  };

  return (
    <aside
      id="anchor-gate"
      className={`region-card gate-card${active ? " is-active" : ""}`}
      data-collapsed={collapsed ? "true" : "false"}
      aria-label="飞书过门"
    >
      <header className="region-head">
        <span className="card-accent" aria-hidden="true" />
        <div className="card-title-wrap">
          <h2 className="region-title" style={{ fontSize: 13.5 }}>
            {TITLE.startsWith("◆ ") ? TITLE.slice(2) : TITLE}
          </h2>
          <p className="gate-sub">下一张卡会推什么（非已推内容）</p>
        </div>
        <div className="card-head-actions">
          <span className="region-count">
            {gate.stats.passed} <em>门内</em>
          </span>
          <button
            type="button"
            className="card-toggle"
            aria-expanded={!collapsed}
            aria-controls="body-gate"
            aria-label={`${collapsed ? "展开" : "折叠"} 飞书过门`}
            onClick={onToggleCollapse}
          >
            <span className="chevron" aria-hidden="true" />
          </button>
        </div>
      </header>

      <div className="card-body" id="body-gate">
        <div
          className="card-body-inner"
          inert={collapsed}
          aria-hidden={collapsed || undefined}
        >
          <div className="gate-body">
            {/* 账目区：切换分段时保持挂载，标题与数字不重排 */}
            <div className="ledger-primary">
              {PRIMARY.map((f) => (
                <div key={f.key}>
                  <div className="k">{f.label}</div>
                  <div className={`v${f.brand ? " is-brand" : ""}`}>{gate.stats[f.key]}</div>
                </div>
              ))}
            </div>
            <div className="ledger-secondary">
              {SECONDARY.map((f) => (
                <div key={f.key}>
                  <div className="k">{f.label}</div>
                  <div className="v">{gate.stats[f.key]}</div>
                </div>
              ))}
            </div>

            {/* 允许的第二处渐变：滑块细微过渡 */}
            <div
              className="segmented"
              data-active={tab}
              role="tablist"
              aria-label="门内门外"
              onKeyDown={onTabKeyDown}
            >
              <span className="segment-indicator" aria-hidden="true" />
              {(["inside", "outside"] as const).map((key) => (
                <button
                  key={key}
                  type="button"
                  ref={(el) => {
                    tabRefs.current[key] = el;
                  }}
                  role="tab"
                  id={`tab-${key}`}
                  aria-selected={tab === key}
                  aria-controls={`panel-${key}`}
                  tabIndex={tab === key ? 0 : -1}
                  onClick={() => onTabChange(key)}
                >
                  {key === "inside" ? "门内" : "门外"}
                </button>
              ))}
            </div>

            <div
              className="gate-panel"
              id="panel-inside"
              role="tabpanel"
              aria-labelledby="tab-inside"
              hidden={!inside}
            >
              <GateGroups rowsFor={rowsFor} open={open} />
            </div>
            <div
              className="gate-panel"
              id="panel-outside"
              role="tabpanel"
              aria-labelledby="tab-outside"
              hidden={inside}
            >
              <GateGroups rowsFor={rowsFor} open={open} />
            </div>
          </div>
          <div className="gate-foot">门内 = 本轮通过集合 · 卡片按集合变化推送</div>
        </div>
      </div>
    </aside>
  );
}

/** 分组清单：有行的组显「标题 N + 行按钮」，空组显一张零值卡（不写原因、不显示空表头）。 */
function GateGroups({
  rowsFor,
  open,
}: {
  rowsFor: (key: keyof Omit<Gate, "stats">) => Row[];
  open: (symbol: string) => void;
}) {
  return (
    <>
      {GROUPS.map((g) => {
        const rows = rowsFor(g.key);
        return (
          <section key={g.key} className="flex flex-col gap-1.5">
            <div className="gate-sec-head">
              <span className="t">{g.title}</span>
              <span className="n">{rows.length}</span>
            </div>
            {rows.length === 0 ? (
              <div className="gate-zero-row">
                本轮无数据 <span>—</span>
              </div>
            ) : (
              <ul className="gate-list m-0 list-none p-0">
                {rows.map((row, i) => {
                  const sym = rowSymbol(row);
                  const name = typeof row.name === "string" ? row.name : "";
                  return (
                    <li key={`${sym}-${i}`}>
                      <button
                        type="button"
                        id={`gate-${sym}`}
                        onClick={() => open(sym)}
                        className="gate-row"
                        title={name ? `${sym} ${name}` : sym}
                      >
                        <span className="sym">{sym}</span>
                        <span className="nm">{name}</span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}
          </section>
        );
      })}
    </>
  );
}
