"use client";
// The top of Step 5: can a run start now, and if not, what stands in the way.
// One block, right under the heading, with the primary action in it (Start for a
// prepared run, "Prepare a run" otherwise), so that a person does not have to
// read the cards below to learn what to do next. Nothing here is folded: the
// reasons are the same rows the readiness list and the start's own checks name
// (`startOff`: a live D or client, a stale read, a changed prepared run, every
// refusing or unknown readiness row), listed again where they matter.
//
// The click on Start is the attestation of what is written beside it (the three
// checks of the start's checklist, what terminal D itself asks, the two checks of
// the arm stack's windows), so those lines stay with the button.
import { ArrowDown, Lock, Play } from "lucide-react";
import { Badge, Button } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { WordedText } from "./action-parts";
import { ConfirmRefusal } from "./confirm-window";
import { wordOf } from "./ctl-text";
import { sayText } from "./marvel0-say";
import type { ReadyView, RunView } from "./autonomy-doc";
import {
  STATE_TONE,
  isLive,
  ownPolicyUnit,
  startOff,
  stateKey,
} from "./autonomy-logic";
import type { AutonomyActions } from "./use-autonomy";
import type { Polled } from "./use-polled";

/** What the click on Start states, beside the button: the three checks of the
 * start's checklist, what terminal D itself asks before it takes the phrase
 * (its `run_confirm_warning`: scene, limits, checkpoints, providers, hardware),
 * and the two checks of the arm stack's windows that stay. The page sends the
 * codes the backend requires: the checklist with the start, the others as the
 * `attest` of the `execute` key. */
const START_STATEMENTS: ReadonlyArray<readonly [string, string]> = [
  ["marvel0.autonomy.ack.", "scene_ok"],
  ["marvel0.autonomy.ack.", "area_clear"],
  ["marvel0.autonomy.ack.", "arm_home"],
  ["marvel0.autonomy.dcheck.", "scene"],
  ["marvel0.autonomy.dcheck.", "limits"],
  ["marvel0.autonomy.dcheck.", "checkpoints"],
  ["marvel0.autonomy.dcheck.", "providers"],
  ["marvel0.autonomy.dcheck.", "hardware"],
  ["marvel0.ctl.attest.", "hands_clear"],
  ["marvel0.ctl.attest.", "no_client"],
];
const START_ATTEST = { scene_ok: true, area_clear: true, arm_home: true };

/** Where the prepare form is: the test id of its element. */
const FORM = '[data-testid="marvel0-autonomy-form"]';

/** Takes the person to the first field of the prepare form. */
function goToForm(): void {
  const form = document.querySelector<HTMLElement>(FORM);
  if (!form) return;
  const calm = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  form.scrollIntoView?.({ block: "start", behavior: calm ? "auto" : "smooth" });
  form.querySelector<HTMLElement>("input")?.focus({ preventScroll: true });
}

export type SummaryState = "live" | "startable" | "blocked" | "not_prepared";

export function StartSummary({
  poll,
  run,
  actions,
  evalLive,
}: {
  poll: Polled<ReadyView>;
  /** The hosted D's document: its `start` block is part of the answer to
   * "may a start be clicked". */
  run: RunView | null;
  actions: AutonomyActions;
  /** The hosted evaluation client is live: only one session at a time. */
  evalLive: boolean;
}) {
  const { t } = useLocale();
  const ready = poll.value;
  const runLive = isLive(run);
  const off = startOff(ready, { evalLive, runLive, stale: !poll.current, run });
  const prepared = ready?.prepared ?? null;
  const state: SummaryState = runLive
    ? "live"
    : !prepared
      ? "not_prepared"
      : off.length === 0
        ? "startable"
        : "blocked";
  const refused = actions.refused?.key === "start" ? actions.refused : null;
  const working = actions.working !== null;
  const ownPolicy = ready ? ownPolicyUnit(ready.rows) : null;
  const headline = !ready
    ? t("marvel0.autonomy.ready.unreadable")
    : state === "live"
      ? sayText(
          {
            key: "marvel0.autonomy.summary.live",
            params: { state: run ? t(stateKey(run.state.id)) : "" },
          },
          t,
        )
      : state === "startable"
        ? sayText(
            {
              key: "marvel0.autonomy.summary.startable",
              params: { name: prepared!.name },
            },
            t,
          )
        : state === "blocked"
          ? sayText(
              {
                key: "marvel0.autonomy.summary.blocked",
                params: { name: prepared!.name },
              },
              t,
            )
          : t("marvel0.autonomy.summary.not_prepared");
  return (
    <section
      className={`rg-asum rg-asum--${state}`}
      data-testid="marvel0-autonomy-summary"
      data-state={state}
      aria-labelledby="marvel0-autonomy-summary-title"
    >
      <div className="rg-actions-row">
        <h4 id="marvel0-autonomy-summary-title" className="rg-marker__title">
          {t("marvel0.autonomy.summary.title")}
        </h4>
        {state === "live" && run && (
          <Badge tone={STATE_TONE[run.state.id]}>
            {t(stateKey(run.state.id))}
          </Badge>
        )}
      </div>
      <p
        className="rg-asum__headline"
        role="status"
        data-testid="marvel0-autonomy-summary-headline"
      >
        {headline}
      </p>
      {state !== "live" && off.length > 0 && (
        <div className="rg-block" data-testid="marvel0-autonomy-start-off">
          <span className="rg-block__label">
            <Lock size={12} aria-hidden="true" />
            {t("marvel0.autonomy.start.off")}
          </span>
          <ul className="rg-block__list">
            {off.map((row) => (
              <li key={row.code} data-code={row.code}>
                <WordedText worded={row.worded} />
              </li>
            ))}
          </ul>
        </div>
      )}
      {state === "not_prepared" && ready && (
        <div className="rg-runstart">
          <Button
            variant="primary"
            icon={ArrowDown}
            onClick={goToForm}
            data-testid="marvel0-autonomy-summary-prepare"
          >
            {t("marvel0.autonomy.summary.prepare")}
          </Button>
          <span className="rg-muted">
            {t("marvel0.autonomy.summary.prepare_hint")}
          </span>
        </div>
      )}
      {(state === "startable" || state === "blocked") && prepared && (
        <>
          <div className="rg-runstart">
            <Button
              variant="primary"
              icon={Play}
              disabled={off.length > 0 || working}
              loading={actions.working === "start"}
              onClick={() => {
                actions.clear();
                // The click is the attestation of everything written beside it
                // (L2-STEP5-SIMPLIFY-1117): the page sends the checklist the
                // backend still requires, and answers D's start prompt itself
                // once D shows it (`use-macro.tsx`, the sequence `start`).
                void actions.start(prepared.name, START_ATTEST);
              }}
              data-testid="marvel0-autonomy-start"
            >
              {t("marvel0.autonomy.start.button")}
            </Button>
          </div>
          <div className="rg-astart" data-testid="marvel0-autonomy-start-says">
            <p className="rg-muted">{t("marvel0.autonomy.start.hazard")}</p>
            <p className="rg-muted">{t("marvel0.autonomy.start.statements")}</p>
            <ul className="rg-notes">
              {START_STATEMENTS.map(([prefix, code]) => (
                <li key={code}>{wordOf(t, prefix, code)}</li>
              ))}
            </ul>
            <p className="rg-muted">{t("marvel0.autonomy.gates_scope")}</p>
          </div>
          {ownPolicy && (
            // One line, next to the button: this start stops the server of step 4.
            <p
              className="rg-muted"
              role="status"
              data-testid="marvel0-autonomy-own-policy"
            >
              {sayText(
                {
                  key: "marvel0.autonomy.start.own_policy",
                  params: { unit: ownPolicy },
                },
                t,
              )}
            </p>
          )}
        </>
      )}
      {refused && (
        // The refusal of the start, in words, under the button (no window).
        <ConfirmRefusal
          failure={refused.failure}
          testId="marvel0-autonomy-start-refused"
        >
          <WordedText worded={refused.worded} />
        </ConfirmRefusal>
      )}
    </section>
  );
}
