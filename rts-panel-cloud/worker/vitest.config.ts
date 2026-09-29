import { defineWorkersConfig } from "@cloudflare/vitest-pool-workers/config";
import { TEST_SECRET } from "./test/secret";

/**
 * 集成测试跑在**真实 workerd + 真实 D1（miniflare）**里，不 mock 掉数据库 ——
 * `ON CONFLICT DO UPDATE` 的幂等性、`ORDER BY date DESC, time DESC` 的取轮语义，
 * 都是只有真 SQLite 才验得住的行为（T0.7/T0.8 的验收就靠这个）。
 */
export default defineWorkersConfig({
  test: {
    poolOptions: {
      workers: {
        // 每次测试前把 migrations/0001_init.sql 跑进一个全新的 D1，
        // 保证「同 date+time 重放」这类幂等断言从空表开始。
        wrangler: { configPath: "./wrangler.jsonc" },
        miniflare: {
          d1Databases: ["DB"],
          // 必须绑 INGEST_SECRET，否则 /api/ingest 的 bearerGuard 拿 "Bearer undefined"
          // 去比 → 所有上报测试都 401（症状：全 POST 用例红，且看不出原因）。
          bindings: { INGEST_SECRET: TEST_SECRET, WEB_ORIGINS: "" },
        },
      },
    },
  },
});
