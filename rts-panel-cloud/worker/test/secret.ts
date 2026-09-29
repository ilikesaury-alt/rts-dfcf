/**
 * 测试用上报密钥。**同时**被 `vitest.config.ts`（Node 侧，绑定进 miniflare）和
 * `api.test.ts`（workerd 侧，构造请求头）引用 —— 两处各写一份字面量必然漂移，
 * 漂移的表现是「全部 POST 测试挂在 401 上」这种难以一眼看出的症状。
 *
 * ⚠ 仅供本地测试。生产密钥走 `wrangler secret put INGEST_SECRET`，与本值无关。
 */
export const TEST_SECRET = "test-secret";
