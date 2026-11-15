/**
 * 标题栏 + 多态状态胶囊（v2 §5.4）。
 *
 * 「滞后」的口径（写死在 `lagRounds`）：以**轮次时间戳**为起点，
 * `floor(已过秒数 / 刷新间隔) - 1`，即「本该又跑了 N 轮而快照没动」。
 * 减 1 是因为当前这一轮本身要占满一个间隔；刚跑完的一轮恒为 0。
 *
 * ⚠ 滞后与「无响应」是**两件事**：前者本地服务在、只是没有新轮；后者请求压根
 *   没回来。v2 把它们拆成两种状态色 + 两种解释句，不再一律常驻黄字。
 *   非交易时段持有一份正常历史快照 ⇒ 中性青蓝信息态（**优先于**交易时段滞后），
 *   不再像坏了。函数体 `lagRounds()` / `sessionLabel()` 本轮不改。
 *
 * 状态判定优先级（§4 状态判定口径不变，只替换视觉/解释/交互）：
 *   服务无响应 > 等待首轮 > 非交易时段历史快照 > 滞后 N 轮 > 本轮新鲜。
 *   判定只消费已有的 `meta / snapshot / error / intervalMs / now` 五类信息，
 *   不新增字段、不新增请求、不改判定口径。
 */

import { useEffect, useRef, type RefObject } from "react";

import type { MetaResponse, RegionsResponse } from "../api/types";
import { BrandMark } from "./icons";

const SHANGHAI_OFFSET = "+08:00";

export function lagRounds(date: string, time: string, intervalMs: number, now: number): number {
  const roundTs = new Date(`${date}T${time}${SHANGHAI_OFFSET}`).getTime();
  if (!Number.isFinite(roundTs) || intervalMs <= 0) return 0;
  const elapsed = now - roundTs;
  if (elapsed <= 0) return 0;
  return Math.max(0, Math.floor(elapsed / intervalMs) - 1);
}

/** 时段标签：由**轮次时间**推出（不看本机当前时间 —— 非交易时段要看的是「这份数据是哪个时段的」）。 */
export function sessionLabel(time: string): string {
  const [h, m] = time.split(":").map(Number);
  if (!Number.isFinite(h) || !Number.isFinite(m)) return "未知时段";
  const minutes = h * 60 + m;
  if (minutes >= 9 * 60 + 30 && minutes <= 11 * 60 + 30) return "早盘";
  if (minutes > 11 * 60 + 30 && minutes < 13 * 60) return "午间休市";
  if (minutes >= 13 * 60 && minutes <= 15 * 60) return "尾盘";
  return "非交易时段";
}

export type StatusKind = "fresh" | "lag" | "history" | "error" | "waiting";

export type StatusInfo = {
  kind: StatusKind;
  lag: number;
  session: string;
  /** 胶囊主行 */
  main: string;
  /** 胶囊副行（轮次/seq，保持原值） */
  sub: string;
  /** 点击展开的一句口径解释 */
  explain: string;
};

const FALLBACK_ROUND = "—";

/** 五态判定（纯函数，App 的读数带与本组件共用，避免两处口径漂移）。 */
export function deriveStatus(
  snapshot: RegionsResponse | null,
  error: string | null,
  intervalMs: number,
  now: number,
): StatusInfo {
  const lag = snapshot ? lagRounds(snapshot.date, snapshot.time, intervalMs, now) : 0;
  const session = snapshot ? sessionLabel(snapshot.time) : "—";
  const round = snapshot ? `${snapshot.date} ${snapshot.time}` : FALLBACK_ROUND;
  const seq = snapshot ? `seq ${snapshot.seq}` : "—";
  const offSession = snapshot ? !["早盘", "尾盘"].includes(session) : false;

  if (error !== null) {
    return {
      kind: "error",
      lag,
      session,
      main: "服务无响应 · 显示最后一份快照",
      sub: `${round} · ${seq}`,
      explain: "本机服务暂时未响应；当前画面保留已读取到的最后一轮快照，不补写行。",
    };
  }
  if (!snapshot) {
    return {
      kind: "waiting",
      lag,
      session,
      main: "等待首轮上报",
      sub: "扫描器在本机运行",
      explain: "扫描器尚未完成首轮上报；首轮快照到达后本页自动呈现。",
    };
  }
  if (offSession) {
    return {
      kind: "history",
      lag,
      session,
      main: "历史快照 · 非交易时段",
      sub: `${round} · ${seq}`,
      explain: "非交易时段展示最后一轮历史快照；轮次、时段与 seq 按快照原值呈现。",
    };
  }
  if (lag > 1) {
    return {
      kind: "lag",
      lag,
      session,
      main: `滞后 ${lag} 轮`,
      sub: "显示最后一轮快照",
      explain: "轮次间隔超过一轮时，画面保留最后一轮快照与 seq 原值。",
    };
  }
  return {
    kind: "fresh",
    lag,
    session,
    main: "本轮新鲜",
    sub: `${seq} · ${snapshot.time}`,
    explain: "当前显示本机扫描器最近一轮快照；轮次、时段与 seq 保持原值。",
  };
}

type Props = {
  meta: MetaResponse | null;
  snapshot: RegionsResponse | null;
  error: string | null;
  intervalMs: number;
  now: number;
  /** 弹层开合由 App 统一持有（Escape 与详情返回共用一个键盘入口，避免双监听抢事件）。 */
  statusOpen: boolean;
  onStatusToggle: () => void;
  onStatusClose: () => void;
  pillRef: RefObject<HTMLButtonElement | null>;
};

export default function StatusBar({
  meta,
  snapshot,
  error,
  intervalMs,
  now,
  statusOpen,
  onStatusToggle,
  onStatusClose,
  pillRef,
}: Props) {
  const info = deriveStatus(snapshot, error, intervalMs, now);
  const wrapRef = useRef<HTMLDivElement | null>(null);

  // 外部点击关闭（不抢焦点；Esc 关闭由 App 统一处理并把焦点还给胶囊）。
  useEffect(() => {
    if (!statusOpen) return;
    const onDown = (e: PointerEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) onStatusClose();
    };
    document.addEventListener("pointerdown", onDown);
    return () => document.removeEventListener("pointerdown", onDown);
  }, [statusOpen, onStatusClose]);

  return (
    <header className="titlebar" role="banner">
      <div className="brand">
        <div className="logo" aria-hidden="true">
          <BrandMark />
        </div>
        <div>
          <div className="brand-name">创业板飙升榜</div>
          <div className="brand-sub">RTS DESKTOP · LOCAL MIRROR</div>
        </div>
      </div>

      <div className="title-meta">
        <span>
          轮次 <b className="mono">{snapshot ? `${snapshot.date} ${snapshot.time}` : meta?.ready === false ? "尚无任何一轮" : FALLBACK_ROUND}</b>
        </span>
        <span className="sep" />
        <span>
          seq <b className="mono">{snapshot ? snapshot.seq : FALLBACK_ROUND}</b>
        </span>
        <span className="sep" />
        <span>{snapshot ? info.session : FALLBACK_ROUND}</span>
      </div>

      <div className="title-spacer" />
      <div className="readonly-chip">
        <i />本机 · 只读
      </div>

      <div className="status-wrap" ref={wrapRef} data-state={info.kind}>
        <button
          type="button"
          className="status-pill"
          ref={pillRef}
          aria-expanded={statusOpen}
          aria-controls="status-pop"
          onClick={onStatusToggle}
        >
          <span className="status-dot" aria-hidden="true" />
          <span className="status-copy" role="status" aria-live="polite">
            <span className="status-main">{info.main}</span>
            <span className="status-sub">{info.sub}</span>
          </span>
          <span className="status-chevron" aria-hidden="true" />
        </button>
        <div
          className="status-pop"
          id="status-pop"
          role="region"
          aria-label="状态说明"
          hidden={!statusOpen}
        >
          <div className="pop-head">
            状态口径 <span>当前形态</span>
          </div>
          <p className="status-explain">{info.explain}</p>
        </div>
      </div>
    </header>
  );
}
