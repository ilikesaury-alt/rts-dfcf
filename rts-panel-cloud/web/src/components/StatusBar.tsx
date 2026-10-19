/**
 * 状态条（验收 3）：轮次时间 / seq / 时段 / 数据滞后 N 轮。
 *
 * 「滞后」的口径（写死在 `lagRounds`）：以**轮次时间戳**为起点，
 * `floor(已过秒数 / 刷新间隔) - 1`，即「本该又跑了 N 轮而快照没动」。
 * 减 1 是因为当前这一轮本身要占满一个间隔；刚跑完的一轮恒为 0。
 *
 * ⚠ 滞后与「无响应」是**两件事**：前者本地服务在、只是没有新轮（非交易时段
 * 主循环 sleep 就属此类，属**正常**）；后者请求压根没回来。两者都变黄
 * （验收 3），但文案不同 —— 混成一句会让用户分不清该等还是该查。
 */

import type { MetaResponse, RegionsResponse } from "../api/types";

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

type Props = {
  meta: MetaResponse | null;
  snapshot: RegionsResponse | null;
  error: string | null;
  intervalMs: number;
  now: number;
};

export default function StatusBar({ meta, snapshot, error, intervalMs, now }: Props) {
  const lag = snapshot ? lagRounds(snapshot.date, snapshot.time, intervalMs, now) : 0;
  const stale = error !== null;
  const warn = stale || lag > 1;

  let note = "本地服务正常";
  if (stale) note = "本地服务无响应 · 显示最后一份快照";
  else if (lag > 1) note = `数据滞后 ${lag} 轮`;
  else if (!snapshot) note = "等待首轮上报";

  return (
    <header className="statusbar" role="banner">
      <span className="statusbar-title">创业板飙升榜看板</span>
      <span className="statusbar-sub">本机 · 只读</span>
      <span className="statusbar-round">
        {snapshot ? (
          <>
            轮次 {snapshot.date} {snapshot.time} · seq {snapshot.seq} · {sessionLabel(snapshot.time)}
          </>
        ) : meta?.ready === false ? (
          "尚无任何一轮"
        ) : (
          "—"
        )}
      </span>
      <span className="statusbar-spacer" />
      <span className={warn ? "statusbar-note statusbar-warn" : "statusbar-note"} role="status">
        {note}
      </span>
    </header>
  );
}
