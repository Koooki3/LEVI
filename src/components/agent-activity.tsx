"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { T, useLocale } from "@/components/levi-locale";

export type ActivityEvent = {
  seq: number;
  at: number;
  tool: string;
  action: string;
  channel: "agent" | "human";
  principal: string;
  status: "started" | "completed" | "failed";
  dataset: string | null;
  run: string | null;
  elapsed_seconds: number | null;
  detail: Record<string, unknown>;
  artifacts: Array<{
    label: string;
    path?: string;
    job?: string;
    detail?: string;
  }>;
  error: string | null;
};

const KEEP = 200;

export type AgentTask = {
  run_id: string;
  dataset: string;
  workflow: string;
  instruction: string;
  episodes: number[];
  completed: number[];
  progress: number;
  status: string;
  waiting_for: string;
  finished: boolean;
  committed: boolean;
  evidence_ready: boolean;
  revision: string | null;
  tokens: number | null;
  token_source: string | null;
  usage_missing: boolean;
  evidence_frames: number | null;
  requests: number | null;
  artifacts: Array<{ label: string; path: string; revision?: string }>;
  last_action: {
    action: string;
    status: string;
    at: number;
    tool: string;
  } | null;
};

/** One action is one row: a "started" is replaced by its own outcome.
 *
 * The journal records both so the panel can show work while it runs; showing
 * them as two entries would make a busy agent unreadable.
 */
function collapse(events: ActivityEvent[]): ActivityEvent[] {
  const rows: ActivityEvent[] = [];
  const openAt = new Map<string, number>();
  for (const event of events) {
    const key = `${event.tool}|${event.run ?? ""}|${JSON.stringify(event.detail)}`;
    if (event.status === "started") {
      openAt.set(key, rows.length);
      rows.push(event);
      continue;
    }
    const index = openAt.get(key);
    if (index === undefined) {
      rows.push(event);
      continue;
    }
    rows[index] = event;
    openAt.delete(key);
  }
  return rows;
}

function relative(at: number, now: number, t: (s: string) => string): string {
  const seconds = Math.max(0, Math.round(now - at));
  if (seconds < 2) return t("just now");
  if (seconds < 60) return `${seconds}${t("s ago")}`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}${t("m ago")}`;
  return `${Math.round(minutes / 60)}${t("h ago")}`;
}

export function useAgentActivity(open: boolean) {
  const [events, setEvents] = useState<ActivityEvent[]>([]);
  const [live, setLive] = useState(false);
  const cursor = useRef(0);

  const absorb = useCallback((incoming: ActivityEvent[]) => {
    if (!incoming.length) return;
    setEvents((previous) => {
      const merged = [...previous];
      for (const event of incoming) {
        if (!merged.some((other) => other.seq === event.seq))
          merged.push(event);
      }
      merged.sort((a, b) => a.seq - b.seq);
      return merged.slice(-KEEP);
    });
    cursor.current = Math.max(cursor.current, ...incoming.map((e) => e.seq));
  }, []);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    void fetch(
      `/api/levi/agent/v1/activity?after=${cursor.current}&limit=${KEEP}`,
    )
      .then((r) => (r.ok ? r.json() : { events: [] }))
      .then((value) => {
        if (!cancelled) absorb(value.events ?? []);
      })
      .catch(() => {});
    const stream = new EventSource(
      `/api/levi/agent/v1/activity/stream?after=${cursor.current}`,
    );
    stream.onopen = () => setLive(true);
    stream.onerror = () => setLive(false);
    stream.onmessage = (message) => {
      try {
        absorb([JSON.parse(message.data) as ActivityEvent]);
      } catch {
        /* a malformed frame must not take the panel down */
      }
    };
    return () => {
      cancelled = true;
      stream.close();
      setLive(false);
    };
  }, [open, absorb]);

  return { events, live };
}

/** Where to look at what a task produced. */
function viewerLink(task: AgentTask): string | null {
  const episode = task.episodes[0];
  if (episode == null) return null;
  return `/${task.dataset}/episode_${episode}`;
}

export default function AgentActivity({ open }: { open: boolean }) {
  const { t } = useLocale();
  const { events, live } = useAgentActivity(open);
  const [tasks, setTasks] = useState<AgentTask[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const finishedOnce = useRef(new Set<string>());
  const [justFinished, setJustFinished] = useState<AgentTask | null>(null);
  const [now, setNow] = useState(() => Date.now() / 1000);
  const [copied, setCopied] = useState<string | null>(null);
  const listRef = useRef<HTMLOListElement | null>(null);
  const follow = useRef(true);

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(timer);
  }, []);

  // Task state comes from the run records, so it is right even if the panel
  // was closed while the work happened. The activity stream only tells us
  // when to look again.
  const loadTasks = useCallback(async () => {
    try {
      const response = await fetch(
        "/api/levi/agent/v1/activity/tasks?limit=12",
      );
      if (!response.ok) return;
      const value = (await response.json()) as { tasks: AgentTask[] };
      setTasks(value.tasks ?? []);
      for (const task of value.tasks ?? []) {
        if (!task.committed) {
          finishedOnce.current.delete(task.run_id);
          continue;
        }
        if (!finishedOnce.current.has(task.run_id)) {
          finishedOnce.current.add(task.run_id);
          setJustFinished(task);
        }
      }
    } catch {
      /* the panel keeps its last known state */
    }
  }, []);

  useEffect(() => {
    if (!open) return;
    void loadTasks();
    const timer = setInterval(() => void loadTasks(), 4000);
    return () => clearInterval(timer);
  }, [open, loadTasks]);

  useEffect(() => {
    if (open) void loadTasks();
  }, [events.length, open, loadTasks]);

  const rows = useMemo(() => {
    const all = collapse(events);
    return selected ? all.filter((row) => row.run === selected) : all;
  }, [events, selected]);

  useEffect(() => {
    if (!follow.current || !listRef.current) return;
    listRef.current.scrollTo({ top: 0, behavior: "smooth" });
  }, [rows.length]);

  // A "started" with no outcome is only in progress for as long as an action
  // plausibly runs; after that its completion was lost, not pending.
  const STALE_AFTER = 180;
  const running = rows.filter(
    (row) => row.status === "started" && now - row.at < STALE_AFTER,
  ).length;
  const paths = useMemo(() => {
    const seen = new Map<string, { label: string; path: string; at: number }>();
    for (const row of rows) {
      for (const artifact of row.artifacts || []) {
        if (artifact.path) {
          seen.set(artifact.path, {
            label: artifact.label,
            path: artifact.path,
            at: row.at,
          });
        }
      }
    }
    return [...seen.values()].sort((a, b) => b.at - a.at).slice(0, 8);
  }, [rows]);

  async function copy(path: string) {
    try {
      await navigator.clipboard.writeText(path);
      setCopied(path);
      setTimeout(
        () => setCopied((value) => (value === path ? null : value)),
        1600,
      );
    } catch {
      setCopied(null);
    }
  }

  return (
    <T>
      <section className="levi-activity" aria-label="Agent activity">
        <header className="levi-activity-head">
          <span className={`levi-activity-pulse ${live ? "live" : ""}`} />
          <strong>{t(live ? "Live" : "Reconnecting…")}</strong>
          <span className="levi-agent-muted">
            {running > 0
              ? `${running} ${t("in progress")}`
              : `${rows.length} ${t("recent actions")}`}
          </span>
        </header>

        {justFinished && (
          <div className="levi-activity-done" role="status">
            <div>
              <strong>{t("Task finished")}</strong>
              <p className="levi-agent-muted">
                {t(justFinished.workflow)} ·{" "}
                {justFinished.dataset.replace(/^local\//, "")} ·{" "}
                {justFinished.episodes.length} {t("episodes")}
                {justFinished.tokens != null &&
                  ` · ${justFinished.tokens.toLocaleString()} ${t("tokens")}`}
              </p>
              {justFinished.artifacts.map((artifact) => (
                <button
                  key={artifact.path}
                  className="levi-activity-path"
                  onClick={() => void copy(artifact.path)}
                >
                  <span className="levi-activity-path-label">
                    {t(artifact.label)}
                  </span>
                  <code>{artifact.path}</code>
                  <span className="levi-activity-copy">
                    {copied === artifact.path ? t("Copied") : t("Copy")}
                  </span>
                </button>
              ))}
            </div>
            <div className="levi-agent-actions">
              {viewerLink(justFinished) && (
                <a
                  className="levi-activity-jump"
                  href={viewerLink(justFinished) as string}
                >
                  {t("Open the result")}
                </a>
              )}
              <button onClick={() => setJustFinished(null)}>
                {t("Dismiss")}
              </button>
            </div>
          </div>
        )}

        {tasks.length > 0 && (
          <div className="levi-activity-tasks">
            <h4>
              {t("Tasks")}
              {selected && (
                <button
                  className="levi-activity-clear"
                  onClick={() => setSelected(null)}
                >
                  {t("Show all activity")}
                </button>
              )}
            </h4>
            {tasks.map((task) => (
              <button
                key={task.run_id}
                className={`levi-activity-task ${
                  selected === task.run_id ? "selected" : ""
                } ${task.committed ? "done" : ""}`}
                aria-pressed={selected === task.run_id}
                onClick={() =>
                  setSelected((value) =>
                    value === task.run_id ? null : task.run_id,
                  )
                }
              >
                <span className="levi-activity-task-head">
                  <span className="levi-activity-action">
                    {t(task.workflow)} · {task.dataset.replace(/^local\//, "")}
                  </span>
                  <span className="levi-activity-chip">
                    {task.completed.length}/{task.episodes.length}
                  </span>
                </span>
                <span className="levi-activity-progress">
                  <span
                    style={{
                      width: `${Math.round((task.committed ? 1 : task.progress) * 100)}%`,
                    }}
                  />
                </span>
                <span className="levi-activity-meta">
                  <span>{t(task.waiting_for)}</span>
                  {task.usage_missing && (
                    <span
                      className="levi-activity-chip"
                      title={t(
                        "LEVI cannot see an external agent's tokens. Ask it to call runs.report_usage so the estimate improves.",
                      )}
                    >
                      {t("usage not reported")}
                    </span>
                  )}
                  {task.tokens != null && (
                    <span
                      title={t(
                        task.token_source === "measured"
                          ? "Metered by LEVI"
                          : "Reported by the agent",
                      )}
                    >
                      {task.tokens.toLocaleString()} {t("tokens")}
                      {task.evidence_frames
                        ? ` · ${task.evidence_frames} ${t("frames")}`
                        : ""}
                    </span>
                  )}
                  {task.last_action && (
                    <span>
                      {t(task.last_action.action)} ·{" "}
                      {relative(task.last_action.at, now, t)}
                    </span>
                  )}
                </span>
              </button>
            ))}
          </div>
        )}

        {paths.length > 0 && (
          <div className="levi-activity-paths">
            <h4>{t("Artifact paths")}</h4>
            {paths.map((artifact) => (
              <button
                key={artifact.path}
                className="levi-activity-path"
                title={t("Copy path")}
                onClick={() => void copy(artifact.path)}
              >
                <span className="levi-activity-path-label">
                  {t(artifact.label)}
                </span>
                <code>{artifact.path}</code>
                <span className="levi-activity-copy">
                  {copied === artifact.path ? t("Copied") : t("Copy")}
                </span>
              </button>
            ))}
          </div>
        )}

        <ol
          className="levi-activity-list"
          ref={listRef}
          onScroll={(e) => {
            follow.current = e.currentTarget.scrollTop < 24;
          }}
        >
          {rows.length === 0 && (
            <li className="levi-agent-muted levi-activity-empty">
              {t("No agent activity yet. Actions appear here as they happen.")}
            </li>
          )}
          {[...rows].reverse().map((row) => (
            <li
              key={row.seq}
              className={`levi-activity-row ${
                row.status === "started" && now - row.at >= STALE_AFTER
                  ? "stale"
                  : row.status
              }`}
            >
              <span
                className={`levi-activity-dot ${
                  row.status === "started" && now - row.at >= STALE_AFTER
                    ? "stale"
                    : row.status
                }`}
              />
              <div className="levi-activity-body">
                <p className="levi-activity-line">
                  <span className={`levi-activity-who ${row.channel}`}>
                    {t(row.channel === "human" ? "You" : "Agent")}
                  </span>
                  <span className="levi-activity-action">{t(row.action)}</span>
                  {typeof row.detail?.apply === "boolean" && (
                    <span className="levi-activity-chip">
                      {t(row.detail.apply ? "applied" : "preview")}
                    </span>
                  )}
                  {typeof row.detail?.episode === "number" && (
                    <span className="levi-activity-chip">
                      {t("episode")} {String(row.detail.episode)}
                    </span>
                  )}
                  {row.dataset && (
                    <span className="levi-activity-chip dataset">
                      {row.dataset.replace(/^local\//, "")}
                    </span>
                  )}
                </p>
                {row.error && (
                  <p className="levi-activity-error">{row.error}</p>
                )}
                <p className="levi-activity-meta">
                  <span>{relative(row.at, now, t)}</span>
                  {row.elapsed_seconds != null && (
                    <span>{row.elapsed_seconds.toFixed(2)}s</span>
                  )}
                  <code>{row.tool}</code>
                </p>
              </div>
            </li>
          ))}
        </ol>
      </section>
    </T>
  );
}
