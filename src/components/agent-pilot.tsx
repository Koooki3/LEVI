"use client";
import { useEffect, useRef, useState } from "react";
import { Eye, Pause, Play, RefreshCw, Send, X } from "lucide-react";
import { useLocale } from "./levi-locale";
import { Button, Card, Field, Input, StatusDot } from "@/components/ds";
import {
  HumanActionMark,
  RequestProblem,
} from "@/components/pages-ui/feedback";
import { Actions, Disclosure, GatedButton, Hint } from "./agent-ui";

type Event = {
  seq: number;
  type: string;
  tool?: string;
  session_id?: string;
  state?: string;
  activity?: unknown;
  evidence?: { episode_index: number; timestamp: number; camera_key: string };
  elapsed_seconds?: number;
};
type Session = {
  id: string;
  run_id: string;
  runtime: string;
  state: string;
  connection: string;
  turns: number;
  reason?: string;
};
type Permission = {
  id: string;
  run_id: string;
  tool: string;
  options: { optionId: string; name: string }[];
};
const base = "/api/levi/agent/v1";
async function request<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(
    base + path,
    body === undefined
      ? { cache: "no-store" }
      : {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
  );
  const value = await response.json();
  if (!response.ok) throw new Error(value.detail || "Pilot request failed");
  return value;
}
export default function AgentPilot({
  runId,
  approved,
  runtime,
  repo,
  edited,
  follow,
}: {
  runId: string;
  approved: boolean;
  runtime?: string;
  repo: string;
  edited: boolean;
  follow: boolean;
}) {
  const { t } = useLocale();
  const [events, setEvents] = useState<Event[]>([]);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [permissions, setPermissions] = useState<Permission[]>([]);
  const [error, setError] = useState("");
  const [text, setText] = useState("");
  const [connected, setConnected] = useState(false);
  const [manifest, setManifest] = useState<{
    artifacts: {
      name: string;
      path: string;
      bytes: number;
      evidence?: Event["evidence"];
    }[];
    status: string;
  } | null>(null);
  const view = useRef({ edited, follow, repo });
  view.current = { edited, follow, repo };
  useEffect(() => {
    let alive = true;
    let cursor = 0;
    let productsDirty = true;
    setEvents([]);
    setManifest(null);
    const receive = (value: Event) => {
      if (!alive || value.seq <= cursor) return;
      cursor = value.seq;
      if (value.type === "action.completed") productsDirty = true;
      setEvents((old) => [...old, value].slice(-300));
      if (value.evidence && view.current.follow && !view.current.edited)
        window.dispatchEvent(
          new CustomEvent("levi-agent-seek", {
            detail: {
              ...value.evidence,
              repo_id: view.current.repo,
              follow: true,
            },
          }),
        );
    };
    const stream = new EventSource(
      `${base}/runs/${encodeURIComponent(runId)}/stream`,
    );
    stream.onopen = () => setConnected(true);
    stream.onerror = () => setConnected(false);
    stream.onmessage = (event) => {
      try {
        receive(JSON.parse(event.data));
      } catch {
        setError("Invalid Pilot event");
      }
    };
    async function refresh() {
      try {
        const [next, pending, replay] = await Promise.all([
          request<Session[]>("/pilot/sessions"),
          request<Permission[]>("/pilot/permissions"),
          request<{ events: Event[] }>("/tools", {
            name: "runs.events",
            arguments: { run_id: runId, after: cursor },
          }),
        ]);
        if (!alive) return;
        setSessions(next.filter((s) => s.run_id === runId));
        setPermissions(pending.filter((s) => s.run_id === runId));
        replay.events.forEach(receive);
        if (productsDirty) {
          productsDirty = false;
          const result = await request<NonNullable<typeof manifest>>(
            `/runs/${encodeURIComponent(runId)}/manifest`,
          );
          if (alive) setManifest(result);
        }
      } catch (e) {
        if (alive) setError(String(e));
      }
    }
    void refresh();
    const timer = setInterval(refresh, 3000);
    return () => {
      alive = false;
      stream.close();
      clearInterval(timer);
    };
  }, [runId]);
  async function act(path: string, body: unknown = {}) {
    setError("");
    try {
      await request(path, body);
    } catch (e) {
      setError(String(e));
    }
  }
  const active = sessions.find((s) => s.connection !== "disconnected");
  return (
    <Card
      padding="compact"
      className="ag-pilot"
      aria-label={t("Pilot activity")}
      title={t("Pilot activity")}
      actions={
        <StatusDot tone={connected ? "info" : "neutral"} live={connected}>
          {t(connected ? "Live" : "Reconnecting / replay")}
        </StatusDot>
      }
    >
      <div className="ag-stack">
        <p className="ag-muted">
          {t(
            runtime
              ? "Managed session: public runtime events and LEVI actions"
              : "API or external session: LEVI actions and artifacts",
          )}
        </p>
        {runtime && (
          <Actions>
            <GatedButton
              size="sm"
              icon={Play}
              reason={
                active
                  ? t("A Pilot session is already running for this task.")
                  : !approved
                    ? t("Approve the execution plan before you start a Pilot.")
                    : null
              }
              onClick={() =>
                void act("/pilot/sessions", { run_id: runId, runtime })
              }
            >
              {t("Start Pilot")}: {runtime}
            </GatedButton>
          </Actions>
        )}
        {sessions.map((s) => (
          <div key={s.id} className="ag-stack">
            <p>
              <strong>{s.runtime}</strong> · {t(s.state)} · {t(s.connection)} ·{" "}
              {s.turns} {t("turns")}
            </p>
            {s.reason && <p role="status">{s.reason}</p>}
            <Actions>
              {s.connection !== "disconnected" ? (
                <>
                  <Button
                    size="sm"
                    icon={Pause}
                    onClick={() => void act(`/pilot/sessions/${s.id}/pause`)}
                  >
                    {t("Take over / pause")}
                  </Button>
                  <Button
                    size="sm"
                    icon={X}
                    onClick={() => void act(`/pilot/sessions/${s.id}/cancel`)}
                  >
                    {t("Cancel")}
                  </Button>
                </>
              ) : (
                <Button
                  size="sm"
                  icon={Play}
                  onClick={() => void act(`/pilot/sessions/${s.id}/resume`)}
                >
                  {t("Resume from task state")}
                </Button>
              )}
            </Actions>
          </div>
        ))}
        {active && (
          <form
            className="ag-form"
            onSubmit={(e) => {
              e.preventDefault();
              void act(`/pilot/sessions/${active.id}/message`, { text });
              setText("");
            }}
          >
            <Field label={t("Message Pilot")}>
              <Input
                value={text}
                maxLength={12000}
                onChange={(e) => setText(e.target.value)}
              />
            </Field>
            <Actions>
              <GatedButton
                type="submit"
                size="sm"
                icon={Send}
                reason={!text.trim() ? t("Write a message to send it.") : null}
              >
                {t("Send")}
              </GatedButton>
            </Actions>
          </form>
        )}
        {permissions.map((p) => (
          <div key={p.id} className="ag-stack">
            <strong>
              {t("Runtime permission")}: {p.tool}
            </strong>
            <Actions>
              <HumanActionMark />
              {p.options.map((o) => (
                <Button
                  size="sm"
                  key={o.optionId}
                  onClick={() =>
                    void act(`/pilot/permissions/${p.id}`, {
                      option_id: o.optionId,
                    })
                  }
                >
                  {o.name}
                </Button>
              ))}
            </Actions>
          </div>
        ))}
        <Hint>
          {t(
            "Following pauses while draft edits are unsaved. Runtime usage may be unknown.",
          )}
        </Hint>
        <ol className="ag-pilot-events" aria-live="polite">
          {events.map((e) => (
            <li key={e.seq}>
              <small>
                #{e.seq} · {e.session_id ? t("Runtime") : t("LEVI core")}
              </small>
              <strong>{e.tool || t(e.type)}</strong>
              {e.state && <span> · {t(e.state)}</span>}
              {e.elapsed_seconds !== undefined && (
                <span> · {e.elapsed_seconds.toFixed(2)}s</span>
              )}
              {e.activity !== undefined && (
                <Disclosure summary={t("Details")}>
                  <pre className="ag-pre">
                    {JSON.stringify(e.activity, null, 2)}
                  </pre>
                </Disclosure>
              )}
            </li>
          ))}
        </ol>
        <Actions>
          <Button
            size="sm"
            icon={RefreshCw}
            onClick={() => {
              void request<typeof manifest>(`/runs/${runId}/manifest`)
                .then(setManifest)
                .catch((e) => setError(String(e)));
            }}
          >
            {t("Refresh artifact manifest")}
          </Button>
        </Actions>
        {manifest && (
          <div className="ag-stack">
            <p>{t(manifest.status)}</p>
            <ul className="ag-list">
              {manifest.artifacts.map((a) => (
                <li key={a.path}>
                  <code>{a.path}</code> · {a.bytes} B
                  {a.evidence && (
                    <GatedButton
                      size="sm"
                      icon={Eye}
                      reason={
                        edited
                          ? t("Save your draft edits before you view evidence.")
                          : null
                      }
                      onClick={() =>
                        window.dispatchEvent(
                          new CustomEvent("levi-agent-seek", {
                            detail: {
                              ...a.evidence,
                              repo_id: repo,
                              follow: true,
                            },
                          }),
                        )
                      }
                    >
                      {t("View evidence")}
                    </GatedButton>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}
        {error && (
          <RequestProblem action="The Pilot request failed" message={error} />
        )}
      </div>
    </Card>
  );
}
