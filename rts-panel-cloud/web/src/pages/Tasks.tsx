/**
 * 桌面版动作面（RTS_TASKS=1 时可用）：本机 AI 分析入口。
 *
 * 主入口命令由服务端 catalog 下发，本文件只渲染、不拼 argv、不重算任何数据。
 * 报告只呈现「执行了什么、结果在哪 / 退出码」，不把正文渲染成判断。
 * 所有请求走 api/client.ts 收口；token 从 health/catalog 的 data 里取。
 *
 * 视觉按 `docs/features/desktop-workbench/prototype.html` 落地（2026-11-15）：
 * 三档按钮（.btn / .btn-primary / .btn-ghost）、卡片色条、状态色徽章、
 * 终端式日志区（时间戳列 + 等宽正文）、任务历史行、空态引导。
 * 原型只提供静态稿，本组件是它的可运行版；差异都记在同目录 SPEC.md。
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { API, apiGet, apiSend } from "../api/client";

type CatalogItem = {
  id: string;
  label: string;
  cmd: string;
  tier: "R" | "N";
  outbound: boolean;
  timeout: number;
  needsConfirm: boolean;
  note: string;
};

type CatalogData = { items: CatalogItem[]; archiveDir: string; token?: string };
type TaskView = {
  id: string;
  catalogId: string;
  command: string;
  tier: string;
  state: string;
  exitCode: number | null;
  timeout: number;
  logDir: string;
  archiveDir: string | null;
  error: { code: string; message: string } | null;
};
type LogData = { offset: number; nextOffset: number; eof: boolean; lines: string[] };

const HEADERS = (token: string): Record<string, string> => ({
  "Content-Type": "application/json",
  "X-RTS-Task-Token": token,
});

const STATE_LABEL: Record<string, string> = {
  queued: "排队中",
  running: "运行中",
  ok: "完成",
  failed: "失败",
  cancelled: "已取消",
};

/** 状态 → 徽章样式档（与 .task-state 的 is-* 对齐）。 */
const STATE_CLASS: Record<string, string> = {
  queued: "is-queued",
  running: "is-running",
  ok: "is-ok",
  failed: "is-failed",
  cancelled: "is-cancelled",
};

export default function Tasks() {
  const [catalog, setCatalog] = useState<CatalogData | null>(null);
  const [token, setToken] = useState("");
  const [selected, setSelected] = useState("");
  const [unavailable, setUnavailable] = useState("");
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [task, setTask] = useState<TaskView | null>(null);
  const [history, setHistory] = useState<TaskView[]>([]);
  const [lines, setLines] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const offsetRef = useRef(0);
  const timerRef = useRef<number | null>(null);

  useEffect(() => {
    let alive = true;
    apiGet<{ alive: boolean; token?: string }>(API.tasksHealth, { headers: { "Content-Type": "application/json" } })
      .then((h) => {
        if (!alive) return;
        setToken(h.token || "");
        return apiGet<CatalogData>(API.tasksCatalog, { headers: HEADERS(h.token || "") });
      })
      .then((c) => {
        if (!alive || !c) return;
        setCatalog(c);
        setToken((t) => t || c.token || "");
        setSelected((s) => s || c.items[0]?.id || "");
      })
      .catch(() => {
        if (alive) setUnavailable("本机动作面未开启（RTS_TASKS）；只读看板照常可用。");
      });
    return () => { alive = false; };
  }, []);

  const poll = useCallback((id: string, tk: string) => {
    if (timerRef.current !== null) window.clearInterval(timerRef.current);
    offsetRef.current = 0;
    setLines([]);
    const tick = () => {
      apiGet<TaskView>(API.tasksStatus(id), { headers: HEADERS(tk) })
        .then((v) => {
          setTask(v);
          // 任务历史：终态任务按执行时序留在列表里（原型 .history-row 语义）
          setHistory((prev) => {
            const rest = prev.filter((t) => t.id !== v.id);
            return ["ok", "failed", "cancelled"].includes(v.state) ? [v, ...rest] : [v, ...rest];
          });
          const done = ["ok", "failed", "cancelled"].includes(v.state);
          if (done && timerRef.current !== null) {
            window.clearInterval(timerRef.current);
            timerRef.current = null;
          }
        })
        .catch(() => {
          if (timerRef.current !== null) {
            window.clearInterval(timerRef.current);
            timerRef.current = null;
          }
        });
      apiGet<LogData>(API.tasksLog(id, offsetRef.current), { headers: HEADERS(tk) })
        .then((l) => {
          offsetRef.current = l.nextOffset;
          if (l.lines.length) setLines((prev) => [...prev, ...l.lines]);
        })
        .catch(() => {});
    };
    tick();
    timerRef.current = window.setInterval(tick, 700);
  }, []);

  useEffect(() => () => {
    if (timerRef.current !== null) window.clearInterval(timerRef.current);
  }, []);

  const submit = useCallback((confirmed: boolean) => {
    const item = catalog?.items.find((c) => c.id === selected);
    if (!item) return;
    setConfirmOpen(false);
    setBusy(true);
    apiSend<TaskView>(API.tasksSubmit, {
      catalogId: item.id,
      confirm: { acknowledged: confirmed },
    }, { headers: HEADERS(token) })
      .then((v) => { setTask(v); poll(v.id, token); })
      .catch(() => setUnavailable("提交失败，请查看本机任务服务日志。"))
      .finally(() => setBusy(false));
  }, [catalog, selected, token, poll]);

  const run = () => {
    const item = catalog?.items.find((c) => c.id === selected);
    if (!item) return;
    if (item.needsConfirm) setConfirmOpen(true);
    else submit(false);
  };

  const cancel = () => {
    if (!task) return;
    apiSend(API.tasksCancel, { id: task.id }, { headers: HEADERS(token) }).catch(() => {});
  };

  if (unavailable) {
    return (
      <div className="detail-workspace">
        <section className="detail-card">
          <div className="detail-card-head">
            <span className="detail-card-accent" />
            <div className="card-title-wrap">
              <h2>AI 分析</h2>
              <p>本机动作面</p>
            </div>
          </div>
          <div className="detail-card-body">
            <p className="state-line">{unavailable}</p>
          </div>
        </section>
      </div>
    );
  }

  const item = catalog?.items.find((c) => c.id === selected);
  const live = task && ["queued", "running"].includes(task.state);

  return (
    <div className="detail-workspace tasks-workspace">
      {/* ── 命令选择 ───────────────────────────────────────────── */}
      <section className="detail-card">
        <div className="detail-card-head">
          <span className="detail-card-accent" />
          <div className="card-title-wrap">
            <h2>命令目录</h2>
            <p>参数来自命令目录 · 服务端按白名单拼装 argv</p>
          </div>
          <a className="ai-btn" href="#/">返回看板</a>
        </div>
        <div className="detail-card-body flex flex-col gap-3">
          <div className="tasks-form">
            <label htmlFor="task-catalog">命令</label>
            <select
              id="task-catalog"
              className="tasks-catalog"
              value={selected}
              onChange={(e) => setSelected(e.target.value)}
            >
              {catalog?.items.map((c) => (
                <option key={c.id} value={c.id}>{c.label}</option>
              ))}
            </select>
            <button type="button" className="btn btn-primary" onClick={run} disabled={busy || !catalog}>
              {busy ? "提交中…" : "执行"}
            </button>
          </div>

          {item ? (
            <>
              <code className="tasks-cmd">{item.cmd}</code>
              <div className="tasks-meta">
                <span className="scope-chip"><i />{item.tier === "R" ? "零出网" : "出网 · 耗凭据"}</span>
                <span className="detail-note">超时 {item.timeout}s</span>
              </div>
              <p className="state-line">{item.note}</p>
            </>
          ) : (
            <p className="state-line">正在读取本机命令目录…</p>
          )}

          {catalog?.archiveDir ? (
            <dl className="tasks-facts">
              <div className="tasks-fact">
                <dt>报告归档</dt>
                <dd>{catalog.archiveDir}</dd>
              </div>
            </dl>
          ) : null}
        </div>
      </section>

      {/* ── 扫描器状态（原型 .scanner-status：只读回显，不做开关） ── */}
      <section className="detail-card">
        <div className="detail-card-head">
          <span className="detail-card-accent" />
          <div className="card-title-wrap">
            <h2>扫描器状态</h2>
            <p>本机扫描器 · 只读回显</p>
          </div>
          <span className="task-state is-queued"><i />本机回环</span>
        </div>
        <div className="detail-card-body">
          <dl className="tasks-facts">
            <div className="tasks-fact">
              <dt>数据来源</dt>
              <dd>本机扫描器最后一轮快照</dd>
            </div>
            <div className="tasks-fact">
              <dt>回环地址</dt>
              <dd>127.0.0.1 · 每 60s 读取一次</dd>
            </div>
            <div className="tasks-fact">
              <dt>口径</dt>
              <dd>轮次、时段与区块口径随快照呈现</dd>
            </div>
          </dl>
        </div>
      </section>

      {/* ── 任务中心 ───────────────────────────────────────────── */}
      <section className="detail-card">
        <div className="detail-card-head">
          <span className="detail-card-accent" />
          <div className="card-title-wrap">
            <h2>任务中心</h2>
            <p>状态、退出码与产出路径按任务逐项呈现</p>
          </div>
          {task ? (
            <span className={`task-state ${STATE_CLASS[task.state] || ""}`}>
              <i />{STATE_LABEL[task.state] || task.state}
            </span>
          ) : null}
          {live ? (
            <button type="button" className="btn btn-sm btn-danger" onClick={cancel}>取消</button>
          ) : null}
        </div>
        <div className="detail-card-body flex flex-col gap-3">
          {task ? (
            <dl className="tasks-facts">
              <div className="tasks-fact">
                <dt>命令</dt>
                <dd>{task.command}</dd>
              </div>
              <div className="tasks-fact">
                <dt>任务号</dt>
                <dd>{task.id}</dd>
              </div>
              <div className="tasks-fact">
                <dt>退出码</dt>
                <dd>{task.exitCode === null ? "—" : task.exitCode}</dd>
              </div>
              {task.logDir ? (
                <div className="tasks-fact">
                  <dt>日志目录</dt>
                  <dd>{task.logDir}</dd>
                </div>
              ) : null}
              {task.archiveDir ? (
                <div className="tasks-fact">
                  <dt>报告归档</dt>
                  <dd>{task.archiveDir}</dd>
                </div>
              ) : null}
            </dl>
          ) : (
            <div className="tasks-empty">
              <div className="tasks-empty-art" aria-hidden="true"><span /><span /><span /></div>
              <div>
                <div className="tasks-empty-title">尚未执行任务</div>
                <div className="tasks-empty-note">从上方命令目录选择一条命令并执行；执行事实、退出码与产出路径会出现在这里。</div>
              </div>
            </div>
          )}
          {task?.error ? (
            <p className="state-line is-error">{task.error.code} · {task.error.message}</p>
          ) : null}
        </div>
      </section>

      {/* ── 逐行日志 ───────────────────────────────────────────── */}
      {task ? (
        <section className="detail-card">
          <div className="detail-card-head">
            <span className="detail-card-accent" />
            <div className="card-title-wrap">
              <h2>逐行日志</h2>
              <p>仅本机执行事实</p>
            </div>
          </div>
          <div className="tasks-log-head">
            <span className="tasks-log-path">{task.logDir ? `${task.logDir}/stdout.log` : "—"}</span>
            <span className="detail-note">{lines.length} 行</span>
          </div>
          <div className="tasks-log-box">
            {lines.length
              ? lines.map((l, i) => (
                  <div className="tasks-log-line" key={i}>
                    <span className="ts">{String(i + 1).padStart(4, " ")}</span>
                    <span>{l}</span>
                  </div>
                ))
              : <div className="tasks-log-empty">等待日志…</div>}
          </div>
        </section>
      ) : null}

      {/* ── 任务历史 ───────────────────────────────────────────── */}
      {history.length ? (
        <section className="detail-card">
          <div className="detail-card-head">
            <span className="detail-card-accent" />
            <div className="card-title-wrap">
              <h2>任务历史</h2>
              <p>本次会话内执行过的命令</p>
            </div>
            <span className="detail-note">{history.length} 条</span>
          </div>
          <div className="detail-card-body">
            <div className="tasks-history">
              {history.map((h) => (
                <div className={`tasks-history-row ${STATE_CLASS[h.state] || ""}`} key={h.id}>
                  <span className="h-dot" />
                  <span className="h-cmd">{h.command}</span>
                  <span className="h-meta">exit {h.exitCode === null ? "—" : h.exitCode}</span>
                </div>
              ))}
            </div>
          </div>
        </section>
      ) : null}

      {confirmOpen && item ? (
        <div className="tasks-mask" role="dialog" aria-modal="true">
          <div className="tasks-dialog">
            <h2>确认执行</h2>
            <p>该命令会联网并消耗本机模型凭据；任务进入单槽串行队列，退出码与日志原样保留。</p>
            <code className="tasks-cmd">{item.cmd}</code>
            <div className="tasks-dialog-row">
              <button type="button" className="btn btn-ghost" onClick={() => setConfirmOpen(false)}>取消</button>
              <button type="button" className="btn btn-primary" onClick={() => submit(true)}>确认并排队</button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
