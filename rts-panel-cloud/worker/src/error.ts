/** 统一错误信封（design §4.1）。所有处理器共用，无一例外。 */
export class AppError extends Error {
  constructor(
    public code: string,
    message: string,
    public status = 400,
  ) {
    super(message);
    this.name = "AppError";
  }
}

export const err = (code: string, message: string) => ({ ok: false, error: { code, message } });
export const ok = (data: unknown) => ({ ok: true, data });
