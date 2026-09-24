"use client";
import { useEffect, useState } from "react";
import {
  readBrowserStorage,
  removeBrowserStorage,
  writeBrowserStorage,
} from "@/utils/browserStorage";
import { T, useLocale } from "./levi-locale";

// The task this viewer last worked on: closing the panel or reloading the
// page must not lose a task that is still running.
const LAST_TASK = "levi-agent-task";

type Step = {
  kind: string;
  state: string;
  run_id?: string;
  waiting_for?: string | null;
  status?: string;
  report?: string;
};
type TaskReport = {
  tokens: number;
  seconds_worked: number;
  wall_seconds: number;
  parts: {
    part: string;
    tokens: number | null;
    token_source: string;
    seconds: number | null;
  }[];
};
type Task = {
  id: string;
  status: string;
  request: string;
  spec: unknown;
  problems: string[];
  steps: Step[];
  interpretation: {
    tokens: number | null;
    token_source: string;
    seconds: number;
  };
  report?: TaskReport;
};

async function call<R>(name: string, args: unknown): Promise<R> {
  const response = await fetch("/api/levi/agent/v1/tools", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, arguments: args }),
  });
  const data = await response.json();
  if (!response.ok)
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : JSON.stringify(data.detail),
    );
  return data as R;
}

/**
 * One sentence in, a checked task out. The local model interprets the
 * request; LEVI checks it against the catalog; the person approves it; each
 * step then stops at the same human gates as a hand-built plan.
 */
export default function AgentTaskConsole({
  providers,
  supervision = "none",
  teacherGrant = "",
}: {
  providers: { name: string; kind?: string }[];
  // The teacher gate chosen under "External teacher supervision" applies to
  // tasks described in words too.
  supervision?: "none" | "shadow" | "supervised";
  teacherGrant?: string;
}) {
  const { t } = useLocale();
  const local = providers.filter(
    (p) => p.kind === "ollama" || p.kind === "openai-local",
  );
  const [provider, setProvider] = useState(local[0]?.name ?? "");
  const [text, setText] = useState("");
  const [task, setTask] = useState<Task | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function act(step: () => Promise<Task>) {
    setBusy(true);
    setError("");
    try {
      const next = await step();
      setTask(next);
      writeBrowserStorage("local", LAST_TASK, next.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    const id = readBrowserStorage("local", LAST_TASK);
    if (!id) return;
    call<Task>("tasks.get", { task_id: id })
      .then(setTask)
      .catch(() => removeBrowserStorage("local", LAST_TASK));
  }, []);

  if (!local.length)
    return (
      <p className="levi-agent-muted">
        <T>
          Configure and bind a local model (Ollama or a local server) to
          describe tasks in words.
        </T>
      </p>
    );
  return (
    <details open>
      <summary>
        <T>Describe a task in words</T>
      </summary>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void act(() =>
            call<Task>("tasks.interpret", {
              text,
              provider: provider || local[0].name,
              supervision: teacherGrant ? supervision : "none",
              teacher_grant: teacherGrant || null,
            }),
          );
        }}
      >
        <label>
          <T>Local model</T>
          <select
            value={provider}
            onChange={(e) => setProvider(e.target.value)}
          >
            {local.map((p) => (
              <option key={p.name} value={p.name}>
                {p.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          <T>Request</T>
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder={t("TASK_REQUEST_EXAMPLE")}
          />
        </label>
        <button disabled={busy || text.trim().length < 3}>
          <T>{busy ? "Working…" : "Interpret"}</T>
        </button>
      </form>
      {error && (
        <p role="alert" className="levi-error">
          {error}
        </p>
      )}
      {task && (
        <div aria-live="polite">
          <p>
            <strong>{task.id}</strong> · <T>{task.status}</T> ·{" "}
            <T>interpretation</T>: {task.interpretation.tokens ?? "?"} tokens (
            {task.interpretation.token_source}), {task.interpretation.seconds}s
          </p>
          {task.problems.length > 0 && (
            <ul className="levi-error">
              {task.problems.map((p) => (
                <li key={p}>{p}</li>
              ))}
            </ul>
          )}
          <pre className="levi-agent-json">
            {JSON.stringify(task.spec, null, 2)}
          </pre>
          <div className="levi-agent-actions">
            {task.status === "awaiting_approval" && (
              <button
                disabled={busy}
                onClick={() =>
                  void act(() =>
                    call<Task>("tasks.approve", { task_id: task.id }),
                  )
                }
              >
                <T>Approve this task</T>
              </button>
            )}
            {["approved", "running"].includes(task.status) && (
              <button
                disabled={busy}
                onClick={() =>
                  void act(() =>
                    call<Task>("tasks.advance", { task_id: task.id }),
                  )
                }
              >
                <T>Continue</T>
              </button>
            )}
            <button
              disabled={busy}
              onClick={() =>
                void act(() => call<Task>("tasks.get", { task_id: task.id }))
              }
            >
              <T>Refresh</T>
            </button>
          </div>
          {task.steps.length > 0 && (
            <ol>
              {task.steps.map((s, i) => (
                <li key={i}>
                  {s.kind} · <T>{s.state}</T>
                  {s.run_id ? ` · ${s.run_id}` : ""}
                  {s.status ? ` · ${s.status}` : ""}
                  {s.waiting_for ? ` · ${s.waiting_for}` : ""}
                </li>
              ))}
            </ol>
          )}
          {task.report && (
            <table>
              <tbody>
                {task.report.parts.map((p) => (
                  <tr key={p.part}>
                    <td>{p.part}</td>
                    <td>
                      {p.tokens ?? "?"} tokens ({p.token_source})
                    </td>
                    <td>{p.seconds ?? "?"} s</td>
                  </tr>
                ))}
                <tr>
                  <th>
                    <T>Total</T>
                  </th>
                  <th>{task.report.tokens} tokens</th>
                  <th>
                    {task.report.seconds_worked} s · <T>wall</T>{" "}
                    {task.report.wall_seconds} s
                  </th>
                </tr>
              </tbody>
            </table>
          )}
        </div>
      )}
    </details>
  );
}
