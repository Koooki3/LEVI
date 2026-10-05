"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowUpRight,
  Bot,
  Check,
  Copy,
  User,
  Activity as ActivityIcon,
} from "lucide-react";
import { T, useLocale } from "@/components/levi-locale";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  Icon,
  StatusDot,
  Tag,
  Tooltip,
} from "@/components/ds";
import { prefersReducedMotion } from "@/lib/design/motion";
import { Actions } from "./agent-ui";

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
    // Glide to the newest row unless motion is reduced (the system setting
    // or the app's own switch, `data-motion="reduce"` on an ancestor).
    listRef.current.scrollTo({
      top: 0,
      behavior: prefersReducedMotion(listRef.current) ? "auto" : "smooth",
    });
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

  const pathButton = (artifact: { label: string; path: string }) => (
    <Button
      key={artifact.path}
      size="sm"
      variant="secondary"
      className="ag-path"
      icon={copied === artifact.path ? Check : Copy}
      onClick={() => void copy(artifact.path)}
    >
      <span className="ag-path__label">{t(artifact.label)}</span>
      <code className="ag-path__code">{artifact.path}</code>
      <span className="ag-path__action">
        {copied === artifact.path ? t("Copied") : t("Copy")}
      </span>
    </Button>
  );

  const rowState = (row: ActivityEvent) =>
    row.status === "started" && now - row.at >= STALE_AFTER
      ? "stale"
      : row.status;

  return (
    <T>
      <section className="ag-activity" aria-label={t("Agent activity")}>
        <header className="ag-activity__head">
          <StatusDot tone={live ? "info" : "neutral"} live={live}>
            <strong>{t(live ? "Live" : "Reconnecting…")}</strong>
          </StatusDot>
          <span className="ag-muted">
            {running > 0
              ? `${running} ${t("in progress")}`
              : `${rows.length} ${t("recent actions")}`}
          </span>
        </header>

        {justFinished && (
          <Card
            padding="compact"
            className="ag-done"
            role="status"
            title={t("Task finished")}
            description={
              <>
                {t(justFinished.workflow)} ·{" "}
                {justFinished.dataset.replace(/^local\//, "")} ·{" "}
                {justFinished.episodes.length} {t("episodes")}
                {justFinished.tokens != null &&
                  ` · ${justFinished.tokens.toLocaleString()} ${t("tokens")}`}
              </>
            }
            actions={
              <Actions>
                {viewerLink(justFinished) && (
                  <a
                    className="ds-btn ds-btn--primary ds-btn--sm ds-focus"
                    href={viewerLink(justFinished) as string}
                  >
                    <span className="ds-btn__label">
                      {t("Open the result")}
                    </span>
                    <Icon icon={ArrowUpRight} />
                  </a>
                )}
                <Button size="sm" onClick={() => setJustFinished(null)}>
                  {t("Dismiss")}
                </Button>
              </Actions>
            }
          >
            {justFinished.artifacts.length > 0 && (
              <div className="ag-paths">
                {justFinished.artifacts.map(pathButton)}
              </div>
            )}
          </Card>
        )}

        {tasks.length > 0 && (
          <div className="ag-activity__tasks">
            <div className="ag-subhead">
              <h4>{t("Tasks")}</h4>
              {selected && (
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => setSelected(null)}
                >
                  {t("Show all activity")}
                </Button>
              )}
            </div>
            {tasks.map((task) => (
              <Button
                key={task.run_id}
                variant="secondary"
                className={`ag-task ${task.committed ? "is-done" : ""}`}
                aria-pressed={selected === task.run_id}
                onClick={() =>
                  setSelected((value) =>
                    value === task.run_id ? null : task.run_id,
                  )
                }
              >
                <span className="ag-task__head">
                  <span className="ag-task__name">
                    {t(task.workflow)} · {task.dataset.replace(/^local\//, "")}
                  </span>
                  <Tag>
                    {task.completed.length}/{task.episodes.length}
                  </Tag>
                </span>
                <span className="ag-bar" aria-hidden="true">
                  <span
                    style={{
                      width: `${Math.round((task.committed ? 1 : task.progress) * 100)}%`,
                    }}
                  />
                </span>
                <span className="ag-task__meta">
                  <span>{t(task.waiting_for)}</span>
                  {task.usage_missing && (
                    <Tooltip
                      content={t(
                        "LEVI cannot see an external agent's tokens. Ask it to call runs.report_usage so the estimate improves.",
                      )}
                    >
                      <span className="ag-has-tip" tabIndex={0}>
                        <Tag>{t("usage not reported")}</Tag>
                      </span>
                    </Tooltip>
                  )}
                  {task.tokens != null && (
                    <Tooltip
                      content={t(
                        task.token_source === "measured"
                          ? "Metered by LEVI"
                          : "Reported by the agent",
                      )}
                    >
                      <span className="ag-has-tip" tabIndex={0}>
                        {task.tokens.toLocaleString()} {t("tokens")}
                        {task.evidence_frames
                          ? ` · ${task.evidence_frames} ${t("frames")}`
                          : ""}
                      </span>
                    </Tooltip>
                  )}
                  {task.last_action && (
                    <span>
                      {t(task.last_action.action)} ·{" "}
                      {relative(task.last_action.at, now, t)}
                    </span>
                  )}
                </span>
              </Button>
            ))}
          </div>
        )}

        {paths.length > 0 && (
          <div className="ag-activity__paths">
            <div className="ag-subhead">
              <h4>{t("Artifact paths")}</h4>
            </div>
            <div className="ag-paths">{paths.map(pathButton)}</div>
          </div>
        )}

        <ol
          className="ag-activity__list ds-focus"
          tabIndex={0}
          ref={listRef}
          onScroll={(e) => {
            follow.current = e.currentTarget.scrollTop < 24;
          }}
        >
          {rows.length === 0 && (
            <li>
              <EmptyState
                icon={ActivityIcon}
                title={t(
                  "No agent activity yet. Actions appear here as they happen.",
                )}
              />
            </li>
          )}
          {[...rows].reverse().map((row) => {
            const state = rowState(row);
            return (
              <li key={row.seq} className={`ag-event is-${state}`}>
                <div className="ag-event__line">
                  <StatusDot
                    tone={
                      state === "completed"
                        ? "success"
                        : state === "failed"
                          ? "danger"
                          : state === "started"
                            ? "info"
                            : "neutral"
                    }
                    live={state === "started"}
                  >
                    <span className="ds-sr-only">
                      {t(
                        state === "completed"
                          ? "Done"
                          : state === "failed"
                            ? "Failed"
                            : state === "started"
                              ? "Running"
                              : "No outcome recorded",
                      )}
                    </span>
                  </StatusDot>
                  <Badge
                    tone={row.channel === "agent" ? "info" : "neutral"}
                    icon={row.channel === "human" ? User : Bot}
                  >
                    {t(row.channel === "human" ? "You" : "Agent")}
                  </Badge>
                  <span className="ag-event__action">{t(row.action)}</span>
                  {typeof row.detail?.apply === "boolean" && (
                    <Tag>{t(row.detail.apply ? "applied" : "preview")}</Tag>
                  )}
                  {typeof row.detail?.episode === "number" && (
                    <Tag>
                      {t("episode")} {String(row.detail.episode)}
                    </Tag>
                  )}
                  {row.dataset && (
                    <Tag>{row.dataset.replace(/^local\//, "")}</Tag>
                  )}
                </div>
                {row.error && <p className="ag-event__error">{row.error}</p>}
                <p className="ag-event__meta">
                  <span>{relative(row.at, now, t)}</span>
                  {row.elapsed_seconds != null && (
                    <span>{row.elapsed_seconds.toFixed(2)}s</span>
                  )}
                  <code>{row.tool}</code>
                </p>
              </li>
            );
          })}
        </ol>
      </section>
    </T>
  );
}
