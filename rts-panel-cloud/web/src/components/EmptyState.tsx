/**
 * 空态（v2 §5.3）：纯 CSS 图形 + 一句口径说明 + 终端同源标记。
 *
 * 三件齐全，不编造原因、不补写行、不把 `—` 改写成 `0`、不渲染空表头。
 * 图形 `aria-hidden`，只有可读文本进无障碍树。区块级 136px / 段级 90px。
 *
 * ⚠ 文案受 grep 守卫管（列名 / 结论词都不许出现），故说明句只写口径边界事实。
 */

const TERMINAL = "—（本轮无数据）";

type Props = {
  /** 段级空态用 90px 紧凑图形；区块级默认 136px。 */
  segment?: boolean;
  /** 口径说明（区块级默认「与上方区块独立」，段级默认「两段并列」）。 */
  note?: string;
};

export default function EmptyState({ segment = false, note }: Props) {
  const text = note ?? (segment ? "该段在本轮口径下没有行；两段并列、互不排名。" : "该区块在本轮口径下没有行；口径与上方区块独立。");
  return (
    <div className={`region-empty${segment ? " is-segment" : ""}`}>
      <div className="empty-art" aria-hidden="true">
        <span className="art-line one" />
        <span className="art-line two" />
        <span className="art-line three" />
      </div>
      <div className="empty-copy">
        <div className="empty-title">本轮无数据</div>
        <div className="empty-note">{text}</div>
      </div>
      <div className="empty-terminal">{TERMINAL}</div>
    </div>
  );
}
