/**
 * 鉴权（design §3.2 D2）。
 *
 * 分工：读接口靠 Cloudflare **Access**（GitHub SSO，见 §3.2 第 1 层，
 * 应用路径 `/*` 但**排除 `/api/ingest`**）；写接口（上报）靠这里的 Bearer 密钥。
 * 排除 ingest 的原因：否则上报器要维护 Service Token，违反 R2「本期不启用」。
 */

/** 常数时间字符串比较（时序侧信道；用 `===` 比密钥虽难利用，但规避零成本）。 */
export function safeEqual(a: string, b: string): boolean {
  // 长度不同直接返回：长度本身不是秘密，比较耗时无法逐字节还原内容。
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

/** `/api/ingest` 用：Bearer 密钥不符 → 401。 */
export function bearerGuard(c: { req: { header: (k: string) => string | undefined }; env: { INGEST_SECRET: string } } & {
  json: (b: unknown, s?: number) => Response;
}): Response | null {
  const got = c.req.header("Authorization") ?? "";
  if (!safeEqual(got, `Bearer ${c.env.INGEST_SECRET}`)) {
    return c.json({ ok: false, error: { code: "UNAUTHORIZED", message: "bad ingest secret" } }, 401);
  }
  return null;
}
