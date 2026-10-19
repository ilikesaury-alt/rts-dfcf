/**
 * 类名合并助手（shadcn/ui 约定的 `lib/utils.ts`）。
 *
 * 为什么是 `clsx` + `tailwind-merge` 而不是字符串拼接：后写的工具类要能**覆盖**
 * 先写的（`cn("px-2", "px-4")` → `px-4`），这是组件允许调用方改样式的必要条件；
 * 纯 clsx 会让两个 padding 都留在 class 里，由 CSS 声明顺序意外决定胜负。
 */

import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
