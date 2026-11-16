/**
 * 品牌色 inline SVG 图标（v2：零新增依赖，图标一律自绘）。
 *
 * 纯展示组件：只输出 path/rect，不含任何文案。颜色用 `currentColor`，
 * 由父级 `.nav-item` / `.logo` 的表面状态决定，便于 hover/选中三态过渡。
 */

import type { ReactElement, SVGProps } from "react";

type Props = SVGProps<SVGSVGElement>;

/** 应用标识：折线 + 基线 + 端点，呼应「飙升」但不编码涨跌语义。 */
export function BrandMark(props: Props) {
  return (
    <svg viewBox="0 0 22 22" fill="none" aria-hidden="true" {...props}>
      <path d="M2.5 15.4L7 9.8l3.2 2.3 5.1-7 4.2 2.5" stroke="#2563eb" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M2.5 18.7h17" stroke="#0e8fa8" strokeWidth="1.2" strokeLinecap="round" opacity=".75" />
      <circle cx="17.3" cy="5.1" r="1.6" fill="#0e8fa8" />
    </svg>
  );
}

/** 侧栏四区块的 18×18 线性图标（stroke 用 currentColor）。 */
export const NAV_ICONS: Record<string, (p: Props) => ReactElement> = {
  pool: (p) => (
    <svg viewBox="0 0 18 18" fill="none" aria-hidden="true" {...p}>
      <rect x="2.5" y="2.5" width="5" height="5" rx="1.3" stroke="currentColor" />
      <rect x="10.5" y="2.5" width="5" height="5" rx="1.3" stroke="currentColor" />
      <rect x="2.5" y="10.5" width="5" height="5" rx="1.3" stroke="currentColor" />
      <rect x="10.5" y="10.5" width="5" height="5" rx="1.3" stroke="currentColor" />
    </svg>
  ),
  hist: (p) => (
    <svg viewBox="0 0 18 18" fill="none" aria-hidden="true" {...p}>
      <path d="M14.4 7.1A5.6 5.6 0 1 0 14 12" stroke="currentColor" strokeLinecap="round" />
      <path d="M14.4 3.4v3.9h-3.9" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  ),
  hot: (p) => (
    <svg viewBox="0 0 18 18" fill="none" aria-hidden="true" {...p}>
      <path d="M2 14.4l4.1-5 3 2.2 4.2-7 2.7 2" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M2 16.2h14" stroke="currentColor" opacity=".6" />
    </svg>
  ),
  gate: (p) => (
    <svg viewBox="0 0 18 18" fill="none" aria-hidden="true" {...p}>
      <path d="M3 15.4V3.2h12v12.2" stroke="currentColor" strokeLinejoin="round" />
      <path d="M6 15.4V6.2h6v9.2" stroke="currentColor" opacity=".7" />
      <path d="M9 6.2v9.2" stroke="currentColor" opacity=".5" />
    </svg>
  ),
};
