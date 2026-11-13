"use client";
import { useEffect, useId, useRef, useState } from "react";
import { Badge, Button, Field, Input, Select } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { Note } from "@/components/pages-ui/feedback";
import { newRequestId } from "./deploy-logic";
import { usePolled } from "./use-polled";
import {
  resetHarnessApi,
  type ResetHarnessApi,
  type ResetHarnessStart,
} from "./reset-harness-api";
import {
  parseResetHarnessCatalog,
  parseResetHarnessRun,
  resetRunActive,
  type ResetHarnessRun,
  type ResetScenario,
} from "./reset-harness-doc";

const statusKeys = {
  queued: "resetHarness.status.queued",
  running: "resetHarness.status.running",
  succeeded: "resetHarness.status.succeeded",
  blocked: "resetHarness.status.blocked",
  failed: "resetHarness.status.failed",
  stopped: "resetHarness.status.stopped",
};
const stageKeys: Record<string, string> = {
  created: "resetHarness.stage.created",
  observe: "resetHarness.stage.observe",
  plan: "resetHarness.stage.plan",
  open_gripper: "resetHarness.stage.open_gripper",
  approach: "resetHarness.stage.approach",
  reset: "resetHarness.stage.reset",
  regrasp: "resetHarness.stage.regrasp",
  lift_verify: "resetHarness.stage.lift_verify",
  handoff: "resetHarness.stage.handoff",
  policy: "resetHarness.stage.policy",
  release: "resetHarness.stage.release",
  final_verify: "resetHarness.stage.final_verify",
  complete: "resetHarness.stage.complete",
};
const eventStatusKeys = {
  running: "resetHarness.event.running",
  succeeded: "resetHarness.event.succeeded",
  blocked: "resetHarness.event.blocked",
  stopped: "resetHarness.event.stopped",
  failed: "resetHarness.event.failed",
};
const holdingKeys = {
  pass: "resetHarness.holding.pass",
  fail: "resetHarness.holding.fail",
  unknown: "resetHarness.holding.unknown",
};
const finalKeys = {
  pass: "resetHarness.final.pass",
  fail: "resetHarness.final.fail",
  unknown: "resetHarness.final.unknown",
};
const seconds = (ms: number) => `${(ms / 1000).toFixed(2)} s`;

function Evidence({ value }: { value: Record<string, unknown> }) {
  const { t } = useLocale();
  return (
    <details className="rg-reset__evidence">
      <summary>{t("resetHarness.evidence")}</summary>
      <pre lang="en">{JSON.stringify(value, null, 2)}</pre>
    </details>
  );
}

/** A keyed child keeps a previous run's poll out of the selected history row. */
function RunDetails({
  api,
  id,
  initial,
  pollMs,
  onChanged,
}: {
  api: ResetHarnessApi;
  id: string;
  initial: ResetHarnessRun | null;
  pollMs: number;
  onChanged: () => Promise<void>;
}) {
  const { t } = useLocale();
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const stopping = useRef(false);
  const requestId = useRef<string | null>(null);
  const alive = useRef(true);
  const highestSeq = useRef(initial?.stage_seq ?? 0);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  const polled = usePolled({
    read: (signal) => api.run(id, signal),
    parse: (data) => {
      const run = parseResetHarnessRun(data);
      if (!run || run.run_id !== id || run.stage_seq < highestSeq.current)
        return null;
      highestSeq.current = run.stage_seq;
      return run;
    },
    pollMs,
    freshMs: Math.max(15000, pollMs * 3),
  });
  const run = polled.value ?? initial;
  const stop = async () => {
    if (stopping.current) return;
    stopping.current = true;
    setBusy(true);
    setProblem(null);
    requestId.current ??= newRequestId();
    try {
      const result = await api.stop(id, { request_id: requestId.current });
      if (!alive.current) return;
      const answer = result.ok ? parseResetHarnessRun(result.data) : null;
      if (!answer || answer.run_id !== id) {
        setProblem(
          result.ok
            ? t("resetHarness.invalid")
            : `${result.code}: ${result.message}`,
        );
      } else {
        requestId.current = null;
        highestSeq.current = Math.max(highestSeq.current, answer.stage_seq);
        await polled.refresh();
        await onChanged();
      }
    } catch {
      if (alive.current) setProblem(t("resetHarness.unavailable"));
    } finally {
      stopping.current = false;
      if (alive.current) setBusy(false);
    }
  };
  if (!run)
    return (
      <Note tone="warning" role="status">
        {t(polled.loaded ? "resetHarness.unavailable" : "resetHarness.loading")}
      </Note>
    );
  return (
    <section
      className="rg-reset__run"
      aria-label={t("resetHarness.result")}
      data-testid="reset-harness-run"
    >
      <div className="rg-reset__actions">
        <Badge
          tone={
            run.status === "blocked" || run.status === "failed"
              ? "warning"
              : "info"
          }
        >
          {t(statusKeys[run.status])}
        </Badge>
        {resetRunActive(run) && (
          <Button
            onClick={() => void stop()}
            loading={busy}
            data-testid="reset-harness-stop"
          >
            {t("resetHarness.stop")}
          </Button>
        )}
        <Button onClick={() => void polled.refresh()}>
          {t("resetHarness.refreshResult")}
        </Button>
      </div>
      {!polled.current && (
        <Note tone="warning" role="status">
          {t("resetHarness.snapshot")}
        </Note>
      )}
      {problem && (
        <Note tone="warning" role="alert">
          {problem}
        </Note>
      )}
      <dl className="rg-reset__facts">
        <div>
          <dt>{t("resetHarness.runId")}</dt>
          <dd>
            <code>{run.run_id}</code>
          </dd>
        </div>
        <div>
          <dt>{t("resetHarness.stage")}</dt>
          <dd>
            {stageKeys[run.stage] ? (
              t(stageKeys[run.stage])
            ) : (
              <code lang="en">{run.stage}</code>
            )}{" "}
            · {run.stage_seq}
          </dd>
        </div>
        <div>
          <dt>{t("resetHarness.seed")}</dt>
          <dd>{run.seed}</dd>
        </div>
        <div>
          <dt>{t("resetHarness.elapsed")}</dt>
          <dd>{seconds(run.elapsed_ms)}</dd>
        </div>
        <div>
          <dt>{t("resetHarness.updated")}</dt>
          <dd>
            <time dateTime={run.updated_at}>{run.updated_at}</time>
          </dd>
        </div>
        <div>
          <dt>{t("resetHarness.usage")}</dt>
          <dd>
            {t("resetHarness.scriptedUsage")}
            <br />
            {t("resetHarness.tokensUnknown")}
          </dd>
        </div>
      </dl>
      <div className="rg-reset__checks">
        <section aria-label={t("resetHarness.target")}>
          <h4>{t("resetHarness.target")}</h4>
          {run.target ? (
            <>
              <p>
                <code>{run.target.object_id}</code> ·{" "}
                <code>{run.target.region_id}</code>
              </p>
              <p>
                <code>
                  {run.target.position_xyz.map((x) => x.toFixed(4)).join(", ")}{" "}
                  m
                </code>
              </p>
              <p>
                {t("resetHarness.frame")}: <code>{run.target.frame_id}</code> ·{" "}
                {t("resetHarness.source")}: <code>{run.target.source}</code>
              </p>
            </>
          ) : (
            <p>{t("resetHarness.noTarget")}</p>
          )}
        </section>
        <section aria-label={t("resetHarness.holding")}>
          <h4>{t("resetHarness.holding")}</h4>
          <p data-testid="reset-harness-holding">
            {t(holdingKeys[run.holding.status])}
          </p>
          <code lang="en">{run.holding.reason}</code>
          <Evidence value={run.holding.evidence} />
        </section>
        <section aria-label={t("resetHarness.handoff")}>
          <h4>{t("resetHarness.handoff")}</h4>
          <p data-testid="reset-harness-handoff">
            {t(
              run.handoff.eligible
                ? "resetHarness.handoffEligible"
                : "resetHarness.handoffBlocked",
            )}
          </p>
          <code lang="en">{run.handoff.reason}</code>
          <p>
            {t("resetHarness.policyEpoch")}:{" "}
            {run.handoff.policy_epoch ?? t("resetHarness.unknown")}
          </p>
          <p>{t("resetHarness.physicalDenied")}</p>
        </section>
        <section aria-label={t("resetHarness.final")}>
          <h4>{t("resetHarness.final")}</h4>
          <p data-testid="reset-harness-final">
            {t(finalKeys[run.final_observation?.status ?? "unknown"])}
          </p>
          {run.final_observation && (
            <>
              <code lang="en">{run.final_observation.reason}</code>
              <Evidence value={run.final_observation.evidence} />
            </>
          )}
        </section>
      </div>
      <h4>{t("resetHarness.events")}</h4>
      <p className="rg-muted">{t("resetHarness.eventsHint")}</p>
      {run.events.length === 0 ? (
        <p>{t("resetHarness.noEvents")}</p>
      ) : (
        <ol className="rg-reset__events" data-testid="reset-harness-events">
          {run.events.map((event) => (
            <li key={event.seq}>
              <div className="rg-reset__eventhead">
                <strong>
                  {event.seq}.{" "}
                  {stageKeys[event.stage] ? (
                    t(stageKeys[event.stage])
                  ) : (
                    <code lang="en">{event.stage}</code>
                  )}
                </strong>
                <span>
                  {t(eventStatusKeys[event.status])} ·{" "}
                  {seconds(event.elapsed_ms)}
                </span>
              </div>
              <code lang="en">{event.reason}</code>
              <Evidence value={event.evidence} />
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

/** Opening this card starts reads only. All writes stay in the offline namespace. */
export function ResetHarnessCard({
  api = resetHarnessApi,
  pollMs = 2000,
}: {
  api?: ResetHarnessApi;
  pollMs?: number;
}) {
  const { t } = useLocale();
  const contentId = useId();
  const [open, setOpen] = useState(false);
  const [scenario, setScenario] = useState<ResetScenario>("normal_random");
  const [seed, setSeed] = useState("0");
  const [selected, setSelected] = useState<string | null>(null);
  const [started, setStarted] = useState<ResetHarnessRun | null>(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const pending = useRef<ResetHarnessStart | null>(null);
  const submitting = useRef(false);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  const catalog = usePolled({
    read: (signal) => api.catalog(signal),
    parse: parseResetHarnessCatalog,
    pollMs: Math.max(5000, pollMs),
    freshMs: 20000,
    enabled: open,
  });
  const scenarios = catalog.value?.scenarios ?? [];
  const history = catalog.value?.runs ?? [];
  const runs =
    started && !history.some((r) => r.run_id === started.run_id)
      ? [started, ...history]
      : history;
  const id = selected ?? runs[0]?.run_id ?? null;
  const initial = runs.find((r) => r.run_id === id) ?? null;
  const seedNumber = /^\d+$/.test(seed) ? Number(seed) : NaN;
  const seedValid =
    Number.isSafeInteger(seedNumber) &&
    seedNumber >= 0 &&
    seedNumber <= 0x7fffffff;
  const allowedScenario = scenarios.some((s) => s.id === scenario);
  const start = async () => {
    if (
      submitting.current ||
      !catalog.current ||
      !seedValid ||
      !allowedScenario
    )
      return;
    submitting.current = true;
    setBusy(true);
    setProblem(null);
    pending.current ??= {
      request_id: newRequestId(),
      scenario_id: scenario,
      seed: seedNumber,
    };
    try {
      const result = await api.start(pending.current);
      if (!alive.current) return;
      const run = result.ok ? parseResetHarnessRun(result.data) : null;
      if (
        !run ||
        run.scenario_id !== pending.current.scenario_id ||
        run.seed !== pending.current.seed
      ) {
        setProblem(
          result.ok
            ? t("resetHarness.invalid")
            : `${result.code}: ${result.message}`,
        );
        if (!result.ok && result.status > 0) pending.current = null;
      } else {
        pending.current = null;
        setStarted(run);
        setSelected(run.run_id);
        await catalog.refresh();
      }
    } catch {
      if (alive.current) setProblem(t("resetHarness.unavailable"));
    } finally {
      submitting.current = false;
      if (alive.current) setBusy(false);
    }
  };
  return (
    <section
      className="rg-reset rg-card"
      data-testid="reset-harness-card"
      aria-label={t("resetHarness.title")}
    >
      <div className="rg-reset__header">
        <Button
          aria-expanded={open}
          aria-controls={contentId}
          onClick={() => setOpen(!open)}
          data-testid="reset-harness-toggle"
        >
          {t("resetHarness.title")}
        </Button>
        <Badge tone="neutral">{t("resetHarness.offline")}</Badge>
      </div>
      {open && (
        <div id={contentId} className="rg-reset__body">
          <Note>{t("resetHarness.boundary")}</Note>
          {catalog.problem && (
            <Note tone="warning" role="status">
              {t("resetHarness.catalogUnavailable")}
            </Note>
          )}
          {(catalog.value?.issues?.length ?? 0) > 0 && (
            <Note tone="warning" role="status">
              <p>{t("resetHarness.catalogIssues")}</p>
              <ul>
                {catalog.value!.issues!.map((issue) => (
                  <li key={`${issue.run_id}:${issue.code}`}>
                    <code lang="en">
                      {issue.run_id}: {issue.code}
                    </code>
                  </li>
                ))}
              </ul>
            </Note>
          )}
          {!catalog.loaded && <p role="status">{t("resetHarness.loading")}</p>}
          <div className="rg-reset__form">
            <Field label={t("resetHarness.scenario")}>
              <Select
                value={scenario}
                disabled={busy || pending.current !== null}
                onChange={(event) =>
                  setScenario(event.target.value as ResetScenario)
                }
              >
                {scenarios.map((s) => (
                  <option key={s.id} value={s.id}>
                    {t(s.label_key)}
                  </option>
                ))}
              </Select>
            </Field>
            <Field
              label={t("resetHarness.seed")}
              hint={t("resetHarness.seedHint")}
              error={!seedValid ? t("resetHarness.seedInvalid") : undefined}
            >
              <Input
                type="number"
                inputMode="numeric"
                min={0}
                max={0x7fffffff}
                step={1}
                value={seed}
                disabled={busy || pending.current !== null}
                onChange={(event) => setSeed(event.target.value)}
              />
            </Field>
          </div>
          <div className="rg-reset__actions">
            <Button
              onClick={() => void start()}
              loading={busy}
              disabled={!catalog.current || !seedValid || !allowedScenario}
              data-testid="reset-harness-start"
            >
              {t(
                pending.current && !busy
                  ? "resetHarness.retryStart"
                  : "resetHarness.start",
              )}
            </Button>
            <Button onClick={() => void catalog.refresh()}>
              {t("resetHarness.refreshHistory")}
            </Button>
          </div>
          {problem && (
            <Note tone="warning" role="alert">
              {problem}
            </Note>
          )}
          {runs.length > 0 ? (
            <Field label={t("resetHarness.history")}>
              <Select
                value={id ?? ""}
                onChange={(event) => setSelected(event.target.value)}
                data-testid="reset-harness-history"
              >
                {runs.map((r) => (
                  <option key={r.run_id} value={r.run_id}>
                    {r.run_id} · {t(statusKeys[r.status])} · {r.seed}
                  </option>
                ))}
              </Select>
            </Field>
          ) : (
            catalog.loaded && <p>{t("resetHarness.noRuns")}</p>
          )}
          {id && (
            <RunDetails
              key={id}
              api={api}
              id={id}
              initial={initial}
              pollMs={pollMs}
              onChanged={catalog.refresh}
            />
          )}
        </div>
      )}
    </section>
  );
}
