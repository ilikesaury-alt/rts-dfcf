/** 让 TS 认识 Vite 的 `?raw` 导入（测试里直接读真实迁移 SQL，见 test/api.test.ts）。 */
declare module "*.sql?raw" {
  const content: string;
  export default content;
}
