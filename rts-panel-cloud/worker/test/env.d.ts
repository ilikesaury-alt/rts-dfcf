/**
 * 告诉 `@cloudflare/vitest-pool-workers` 测试里 `env` 的绑定类型来自本项目的 `Env`
 * （否则 `env.DB` 报 TS2339）。标准做法：模块内做接口合并。
 */
import type { Env } from "../src/env";

declare module "cloudflare:test" {
  // eslint-disable-next-line @typescript-eslint/no-empty-interface
  interface ProvidedEnv extends Env {}
}
