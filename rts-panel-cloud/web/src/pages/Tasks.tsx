/**
 * 桌面版动作面（RTS_TASKS=1 时可用）：本机 AI 分析入口。
 *
 * 主入口命令由服务端 catalog 下发，本文件只渲染、不拼 argv、不重算任何数据。
 * 报告只呈现「执行了什么、结果在哪 / 退出码」，不把正文渲染成判断。
 * 所有请求走 api/client.ts 收口；token 从 health/catalog 的 data 里取。
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

export default function Tasks() {
  const [catalog, setCatalog] = useState<CatalogData | null>(null);
  const [token, setToken] = useState("");
  const [selected, setSelected] = useState("");
  const [unavailable, setUnavailable] = useState("");
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [task, setTask] = useState<TaskView | null>(null);
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
          <div className="detail-card-head"><span className="card-accent" /><div className="card-title-wrap"><h2>AI 分析</h2><p>本机动作面</p></div></div>
          <div className="detail-card-body"><p className="state-line">{unavailable}</p></div>
        </section>
      </div>
    );
  }

  const item = catalog?.items.find((c) => c.id === selected);
  return (
    <div className="detail-workspace tasks-workspace">
      <section className="detail-card">
        <div className="detail-card-head">
          <span className="card-accent" />
          <div className="card-title-wrap"><h2>AI 分析</h2><p>执行本机命令 · 结果只呈现执行事实与产出位置</p></div>
          <a className="ai-btn" href="#/">返回看板</a>
        </div>
        <div className="detail-card-body flex flex-col gap-3">
          <div className="tasks-form">
            <label htmlFor="task-catalog">命令</label>
            <select id="task-catalog" value={selected} onChange={(e) => setSelected(e.target.value)}>
              {catalog?.items.map((c) => <option key={c.id} value={c.id}>{c.label}</option>)}
            </select>
            <button type="button" className="ai-btn" onClick={run} disabled={busy || !catalog}>
              {busy ? "提交中…" : "执行"}
            </button>
          </div>
          {item ? (
            <div className="tasks-meta">
              <code>{item.cmd}</code>
              <span className="scope-chip"><i />{item.tier === "R" ? "零出网" : "出网 · 耗凭据"}</span>
              <span className="detail-note">超时 {item.timeout}s</span>
            </div>
          ) : null}
          {item ? <p className="state-line">{item.note}</p> : <p className="state-line">正在读取本机命令目录…</p>}
          {catalog?.archiveDir ? <p className="state-line">报告归档目录：{catalog.archiveDir}</p> : null}
        </div>
      </section>

      {task ? (
        <section className="detail-card">
          <div className="detail-card-head">
            <span className="card-accent" />
            <div className="card-title-wrap"><h2>任务中心</h2><p>状态与日志逐轮回显</p></div>
            <span className="round-pill">{STATE_LABEL[task.state] || task.state}</span>
            {["running", "queued"].includes(task.state) ? (
              <button type="button" className="ai-btn" onClick={cancel}>取消</button>
            ) : null}
          </div>
          <div className="detail-card-body flex flex-col gap-2">
            <p className="state-line"><code>{task.command}</code></p>
            <p className="state-line">任务 {task.id} · 退出码 {task.exitCode === null ? "—" : task.exitCode}</p>
            {task.logDir ? <p className="state-line">日志：{task.logDir}</p> : null}
            {task.archiveDir ? <p className="state-line">报告归档：{task.archiveDir}</p> : null}
            {task.error ? <p className="state-line is-error">{task.error.code} · {task.error.message}</p> : null}
            <pre className="tasks-log">{lines.length ? lines.join("\n") : "等待日志…"}</pre>
          </div>
        </section>
      ) : null}

      {confirmOpen && item ? (
        <div className="tasks-mask" role="dialog" aria-modal="true">
          <div className="tasks-dialog">
            <h2>确认执行</h2>
            <p>该命令会联网并消耗本机模型凭据；任务进入单槽串行队列，退出码与日志原样保留。<code>{item.cmd}</code></p>
            <div className="tasks-dialog-row">
              <button type="button" className="ai-btn" onClick={() => setConfirmOpen(false)}>取消</button>
              <button type="button" className="ai-btn" onClick={() => submit(true)}>确认并排队</button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
