"use client";
import { useEffect, useState } from "react";
import { useLocale } from "@/components/levi-locale";
import type { RunView } from "./autonomy-doc";
import { RUN_FRESH_MS } from "./autonomy-logic";

export const LOOP_PHASES = [
  "prepare",
  "inventory",
  "release",
  "forward",
  "reverse",
  "ended",
] as const;
export type LoopPhase = (typeof LOOP_PHASES)[number];
const PHASE_LABELS: Record<LoopPhase, string> = {
  prepare: "marvel0.autonomy.overview.phase.prepare",
  inventory: "marvel0.autonomy.overview.phase.inventory",
  release: "marvel0.autonomy.overview.phase.release",
  forward: "marvel0.autonomy.overview.phase.forward",
  reverse: "marvel0.autonomy.overview.phase.reverse",
  ended: "marvel0.autonomy.overview.phase.ended",
};
const FACT_LABELS = {
  round: "marvel0.autonomy.overview.round",
  step: "marvel0.autonomy.overview.step",
  done: "marvel0.autonomy.overview.done",
  discarded: "marvel0.autonomy.overview.discarded",
};

/** A current phase, never a claim that earlier phases succeeded. A round can
 * be abandoned or repeated; waits and unrecognised states have no inferred phase. */
export function loopPhase(run: RunView, current: boolean): LoopPhase | null {
  if (!current || run.state.foreign) return null;
  if (run.state.id === "ended") return run.d.alive ? null : "ended";
  if (!run.d.alive) return null;
  switch (run.state.id) {
    case "launching":
    case "await_phrase":
    case "await_phrase_variant":
    case "services":
    case "await_home_o":
    case "await_home_p":
    case "await_home_h":
      return "prepare";
    case "await_inventory":
      return "inventory";
    case "await_release":
      return "release";
    case "forward_running":
      return "forward";
    case "reverse_running":
      return "reverse";
    default:
      return null;
  }
}

function LoopStages({
  phase = null,
  preview = false,
}: {
  phase?: LoopPhase | null;
  preview?: boolean;
}) {
  const { t } = useLocale();
  return (
    <ol className="rg-aloop" aria-label={t("marvel0.autonomy.overview.flow")}>
      {LOOP_PHASES.map((id, i) => (
        <li key={id} aria-current={phase === id ? "step" : undefined}>
          <span className="rg-aloop__number" aria-hidden="true">
            {i + 1}
          </span>
          <span>
            {t(
              preview && id === "ended"
                ? "marvel0.autonomy.plan.ended"
                : PHASE_LABELS[id],
            )}
          </span>
        </li>
      ))}
    </ol>
  );
}

/** A static explanation of the loop, with no run facts or actions. */
export function AutonomyPlan() {
  const { t } = useLocale();
  return (
    <section
      className="rg-aoverview"
      aria-labelledby="marvel0-autonomy-plan-title"
      data-testid="marvel0-autonomy-plan"
    >
      <h4 id="marvel0-autonomy-plan-title" className="rg-marker__title">
        {t("marvel0.autonomy.plan.title")}
      </h4>
      <p className="rg-muted rg-aoverview__note">
        {t("marvel0.autonomy.plan.note")}
      </p>
      <LoopStages preview />
    </section>
  );
}

export function AutonomyOverview({
  run,
  current,
  lastOk,
}: {
  run: RunView;
  current: boolean;
  lastOk: number | null;
}) {
  const { t } = useLocale();
  const [now, setNow] = useState(Date.now);
  // A hanging poll must expire independently of the card's key countdowns.
  useEffect(() => {
    setNow(Date.now());
    if (!current || lastOk === null) return;
    const timer = setTimeout(
      () => setNow(Date.now()),
      Math.max(0, lastOk + RUN_FRESH_MS - Date.now() + 1),
    );
    return () => clearTimeout(timer);
  }, [current, lastOk]);
  const age = lastOk === null ? Infinity : Math.max(now, Date.now()) - lastOk;
  const fresh = current && age >= 0 && age <= RUN_FRESH_MS;
  const phase = loopPhase(run, fresh);
  const p = run.progress;
  const facts = [
    ["round", p?.round, p?.rounds],
    ["step", p?.step, p?.maxSteps],
    ["done", p?.done],
    ["discarded", p?.discarded],
  ] as const;
  return (
    <section
      className="rg-aoverview"
      aria-labelledby="marvel0-autonomy-overview-title"
      data-testid="marvel0-autonomy-overview"
      data-current={fresh}
    >
      <div className="rg-aoverview__heading">
        <h4 id="marvel0-autonomy-overview-title" className="rg-marker__title">
          {t("marvel0.autonomy.overview.title")}
        </h4>
        <span className="rg-muted">
          {t(
            !fresh
              ? "marvel0.autonomy.overview.stale"
              : phase === null
                ? "marvel0.autonomy.overview.unmapped"
                : "marvel0.autonomy.overview.current",
          )}
        </span>
      </div>
      {run.task && <p className="rg-aoverview__task">{run.task}</p>}
      <LoopStages phase={phase} />
      <p className="rg-muted rg-aoverview__note">
        {t("marvel0.autonomy.overview.loop_note")}
      </p>
      <dl className="rg-aoverview__facts">
        {facts.map(([id, value, max]) => (
          <div key={id}>
            <dt>{t(FACT_LABELS[id])}</dt>
            <dd data-testid={`marvel0-autonomy-overview-${id}`}>
              {value ?? "—"}
              {(id === "round" || id === "step") && ` / ${max ?? "—"}`}
            </dd>
          </div>
        ))}
      </dl>
    </section>
  );
}
