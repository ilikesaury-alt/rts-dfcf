/**
 * 红线守卫的**对抗性**测试（2026-11-02，交叉对抗审查 A/P1-1 驱动）。
 *
 * 背景：原实现用逐字正则匹配，对平凡变体（一个全角冒号、一个空格、一句英文）
 * 完全不设防 —— 实测 5/5 逃逸。本文件把这些逃逸例句**固化为回归**，
 * 并额外覆盖「归一化后仍不得误杀合法解读」。
 */
import { describe, expect, it } from "vitest";
import { REFUSAL, isRedlined, normalize } from "../src/explain/guard";
import { PRIORS } from "../src/explain/material";

describe("红线词表：平凡变体必须仍然拦截", () => {
  const must_block = [
    "建议：逢低分批买入，冲高卖出。", // 全角冒号分隔
    "建 议买入", // 词内插空格
    "建议:立即 买 入", // 冒号 + 空格 + 词内空格
    "短期看多，第一目标位 18.5 元，18 元是合理价。", // 目标位/合理价/看多
    "当前可轻仓参与，半仓滚动操作。", // 轻仓/半仓
    "明天大概率冲高，回调就是买点。", // 预测 + 买点
    "Intraday: buy zone 17.8-18.2, sell above 20, stop at 16.9.", // 全英文
    "buy at 17.5", // 英文祈使
    "推荐 300319 目标价 18 元", // 目标价变体
    "止盈位 20.5 元", // 止盈变体
  ];
  for (const s of must_block) {
    it(`拦截：${s.slice(0, 24)}`, () => {
      expect(isRedlined(s)).toBe(true);
    });
  }
});

describe("红线词表：合法解读不得误杀", () => {
  const must_pass = [
    "该票本轮进入动量类池选，评分 61，排名第 17，板块软件。", // 正常解读
    "出现史 1 条，最近一次是 2026-11-01 的动量类。",
    "该桶的评分反指：低分档命中更高，不能按分越高越好读。", // 含「推荐」？不含
    "类别先验：反弹类（代号 RBD），前期回调后出现反弹形态。",
    "K 线共 120 根（2026-01-02 ~ 2026-11-02），最新交易日 2026-11-02。",
  ];
  for (const s of must_pass) {
    it(`放行：${s.slice(0, 20)}`, () => {
      expect(isRedlined(s)).toBe(false);
    });
  }
});

describe("类别先验文案与红线守卫互斥（审查 A / P1-1 连带项）", () => {
  // 类别先验文案会**作为素材发给模型**。若文案本身含红线词：
  // ① 模型转述时必然带着它 → 输出被整段拒答 ⇒ 该类别永远无法解读；
  // ② 若守卫反过来为了放行它而放松，就破坏了红线本身。故两者必须互斥。
  it("PRIORS 的每一条文案都不得触红线", () => {
    const entries = Object.entries(PRIORS);
    expect(entries.length).toBeGreaterThan(0); // 防空转：常量被误改空也能发现
    for (const [cat, text] of entries) {
      expect(isRedlined(text)).toBe(false);
    }
  });

  it("合法解读例句集（含数字/日期/类别名/先验）全部放行", () => {
    const legit = [
      "该票本轮进入池选区，策略标签为动量，涨幅 +3.21%，排名第 17。",
      "评分 61.0，其中 momentum=30.0, flow=20.0, structure=11.0。",
      "出现史 1 条：2026-11-01 10:00:00 momentum score=55.0 pct=+6.20%。",
      "K 线共 2 根（2026-10-31 ~ 2026-11-02），最新交易日 2026-11-02。",
      "最近一根：2026-11-02 开11.00 收12.00 低10.80 高12.40 量1200。",
      PRIORS.momentum ?? "",
    ].filter(Boolean);
    for (const s of legit) expect(isRedlined(s)).toBe(false);
  });
});

describe("归一化", () => {  it("去空白、全角转半角、大写转小写", () => {
    expect(normalize("ＢＵＹ")).toBe("buy");
    expect(normalize("建 议买 入")).toBe("建议买入");
    // 标点也归一化掉（“建议：买入” → “建议买入”，正是绕过防护的形态）
    expect(normalize("目标价：18")).toBe("目标价18");
  });

  it("归一化不改变判定的可解释性：拦截后返回拒答文案而非原文", () => {
    expect(REFUSAL).toContain("投资建议");
    // 拒答文案**可以**列举被拦的类别（用户要知道被拦了什么），
    // 但**不得**出现祈使动作（“请买入”“建议加仓”）。
    expect(REFUSAL).not.toMatch(/请(买入|卖出|加仓|减仓)|建议(买入|卖出|加仓|减仓)|可(买入|卖出)/);
  });
});
