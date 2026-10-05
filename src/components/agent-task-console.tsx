"use client";
import { useEffect, useState } from "react";
import {
  readBrowserStorage,
  removeBrowserStorage,
  writeBrowserStorage,
} from "@/utils/browserStorage";
import { Check, ChevronsRight, RefreshCw, Sparkles } from "lucide-react";
import { T, useLocale } from "./levi-locale";
import {
  Badge,
  Button,
  Field,
  Select,
  Table,
  Textarea,
  TableRow,
} from "@/components/ds";
import {
  HumanActionMark,
  Note,
  RequestProblem,
} from "@/components/pages-ui/feedback";
import { Actions, Disclosure, GatedButton } from "./agent-ui";

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
      <Note tone="info">
        <T>
          Configure and bind a local model (Ollama or a local server) to
          describe tasks in words.
        </T>
      </Note>
    );
  return (
    <Disclosure
      defaultOpen
      icon={Sparkles}
      summary={t("Describe a task in words")}
    >
      <div className="ag-stack">
        <form
          className="ag-form"
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
          <Field label={t("Local model")}>
            <Select
              value={provider}
              onChange={(e) => setProvider(e.target.value)}
            >
              {local.map((p) => (
                <option key={p.name} value={p.name}>
                  {p.name}
                </option>
              ))}
            </Select>
          </Field>
          <Field label={t("Request")}>
            <Textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder={t("TASK_REQUEST_EXAMPLE")}
            />
          </Field>
          <Actions>
            <GatedButton
              type="submit"
              size="sm"
              loading={busy}
              reason={
                text.trim().length < 3
                  ? t("Write at least three characters to interpret.")
                  : null
              }
            >
              {t(busy ? "Working…" : "Interpret")}
            </GatedButton>
          </Actions>
        </form>
        {error && (
          <RequestProblem
            action="The task request did not complete"
            message={error}
          />
        )}
        {task && (
          <div className="ag-stack" aria-live="polite">
            <p>
              <strong>{task.id}</strong>{" "}
              <Badge tone="neutral">{t(task.status)}</Badge>{" "}
              <span className="ag-muted">
                {t("interpretation")}: {task.interpretation.tokens ?? "?"}{" "}
                tokens ({task.interpretation.token_source}),{" "}
                {task.interpretation.seconds}s
              </span>
            </p>
            {task.problems.length > 0 && (
              <Note tone="warning" role="alert">
                <ul className="ag-list">
                  {task.problems.map((p) => (
                    <li key={p}>{p}</li>
                  ))}
                </ul>
              </Note>
            )}
            <pre className="ag-pre">{JSON.stringify(task.spec, null, 2)}</pre>
            <Actions>
              {task.status === "awaiting_approval" && (
                <>
                  <HumanActionMark />
                  <Button
                    size="sm"
                    variant="primary"
                    icon={Check}
                    disabled={busy}
                    onClick={() =>
                      void act(() =>
                        call<Task>("tasks.approve", { task_id: task.id }),
                      )
                    }
                  >
                    {t("Approve this task")}
                  </Button>
                </>
              )}
              {["approved", "running"].includes(task.status) && (
                <Button
                  size="sm"
                  icon={ChevronsRight}
                  disabled={busy}
                  onClick={() =>
                    void act(() =>
                      call<Task>("tasks.advance", { task_id: task.id }),
                    )
                  }
                >
                  {t("Continue")}
                </Button>
              )}
              <Button
                size="sm"
                icon={RefreshCw}
                disabled={busy}
                onClick={() =>
                  void act(() => call<Task>("tasks.get", { task_id: task.id }))
                }
              >
                {t("Refresh")}
              </Button>
            </Actions>
            {task.steps.length > 0 && (
              <ol className="ag-list">
                {task.steps.map((s, i) => (
                  <li key={i}>
                    {s.kind} · {t(s.state)}
                    {s.run_id ? ` · ${s.run_id}` : ""}
                    {s.status ? ` · ${s.status}` : ""}
                    {s.waiting_for ? ` · ${s.waiting_for}` : ""}
                  </li>
                ))}
              </ol>
            )}
            {task.report && (
              <Table density="compact" caption={t("Cost of this task")}>
                <tbody>
                  {task.report.parts.map((p) => (
                    <TableRow key={p.part}>
                      <td>{p.part}</td>
                      <td className="ds-num">
                        {p.tokens ?? "?"} tokens ({p.token_source})
                      </td>
                      <td className="ds-num">{p.seconds ?? "?"} s</td>
                    </TableRow>
                  ))}
                  <TableRow>
                    <th scope="row">{t("Total")}</th>
                    <th className="ds-num">{task.report.tokens} tokens</th>
                    <th className="ds-num">
                      {task.report.seconds_worked} s · {t("wall")}{" "}
                      {task.report.wall_seconds} s
                    </th>
                  </TableRow>
                </tbody>
              </Table>
            )}
          </div>
        )}
      </div>
    </Disclosure>
  );
}
