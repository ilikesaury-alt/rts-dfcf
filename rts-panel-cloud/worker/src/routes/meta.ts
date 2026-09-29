/**
 * `GET /api/meta` —— 状态条数据（design §4.4 / 复核修 9）。
 *
 * 复核修 9：原稿标「0 查询」是错的，这里**1 次查询**读最新快照的
 * `seq/date/time/ingested_at`。状态条要显示「入库时间」和「滞后 N 轮」，
 * 这些都只能从库里读。
 *
 * 取最新轮一律 `ORDER BY date DESC, time DESC`（**不是** seq）。
 */
import type { Env } from "../env";
import { AppError } from "../error";

type Ctx<E> = {
  env: E;
  json: (b: unknown, s?: number) => Response;
};

export async function meta<E extends Env>(c: Ctx<E>): Promise<Response> {
  const row = await c.env.DB.prepare(
    `SELECT date, time, seq, ingested_at FROM snapshots ORDER BY date DESC, time DESC LIMIT 1`,
  ).first<{ date: string; time: string; seq: number; ingested_at: string }>();

  // ⚠ 与 /api/regions 不同：**没有快照不是错误**。面板首次打开时 meta 先行，
  // 此时应显示「等待首轮上报」而不是红色 404。
  if (!row) {
    return c.json({ ok: true, data: { ready: false, date: null, time: null, seq: null, ingestedAt: null } });
  }
  return c.json({ ok: true, data: { ready: true, ...row } });
}
