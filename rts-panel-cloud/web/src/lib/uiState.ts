/**
 * 视图偏好持久化（v2 交互）。
 *
 * 只记**表现层**偏好（侧栏折叠 / 卡片折叠 / 分段位置），一块 `localStorage` 键，
 * 不扩张成状态库、不写任何业务字段、不新增请求。读取失败（隐私模式/损坏 JSON）
 * 一律回退默认值，不让界面白屏。与 `App.tsx` 的分栏宽度键互不干扰。
 */

import { useCallback, useState } from "react";

const STORE_KEY = "rts-desktop-v2";

export type UiState = {
  sidebarCollapsed: boolean;
  collapsed: Record<string, boolean>;
  gateTab: "inside" | "outside";
};

const DEFAULT: UiState = { sidebarCollapsed: false, collapsed: {}, gateTab: "inside" };

function read(): UiState {
  try {
    const saved = JSON.parse(window.localStorage.getItem(STORE_KEY) || "{}") as Partial<UiState>;
    return {
      sidebarCollapsed:
        typeof saved.sidebarCollapsed === "boolean" ? saved.sidebarCollapsed : DEFAULT.sidebarCollapsed,
      collapsed:
        saved.collapsed && typeof saved.collapsed === "object" ? { ...saved.collapsed } : {},
      gateTab: saved.gateTab === "outside" ? "outside" : "inside",
    };
  } catch {
    return { ...DEFAULT, collapsed: {} };
  }
}

function write(next: UiState): void {
  try {
    window.localStorage.setItem(STORE_KEY, JSON.stringify(next));
  } catch {
    // 隐私模式 / 配额满：偏好不落盘，界面行为照常。
  }
}

/** 单一偏好键 + 局部更新（函数式 setState，避免并发轮次互相覆盖）。 */
export function useUiState() {
  const [ui, setUi] = useState<UiState>(read);

  const update = useCallback((patch: Partial<UiState>) => {
    setUi((prev) => {
      const next = { ...prev, ...patch };
      write(next);
      return next;
    });
  }, []);

  const toggleCard = useCallback((key: string) => {
    setUi((prev) => {
      const collapsed = { ...prev.collapsed, [key]: !prev.collapsed[key] };
      const next = { ...prev, collapsed };
      write(next);
      return next;
    });
  }, []);

  return { ui, update, toggleCard };
}
