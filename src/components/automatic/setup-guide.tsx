"use client";
// Step 1 of the evaluation wizard: getting the environment ready. It lists
// what GET /automatic/setup-guide returns, level by level. A command is only
// ever shown for a person to copy and run; LEVI runs nothing here unless an
// executor is given for that step. The emergency stop, the robot's Desk page
// and the placing of objects are done by a person at the robot.
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  CircleCheck,
  CircleHelp,
  ListTodo,
  OctagonAlert,
  TriangleAlert,
} from "lucide-react";
import { Badge, Button, Checkbox, Icon, Skeleton } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { Note, RequestProblem } from "@/components/pages-ui/feedback";
import {
  readBrowserStorage,
  writeBrowserStorage,
} from "@/utils/browserStorage";
import { SetupCommand } from "./setup-copy";
import {
  LEVEL_KEYS,
  SAFETY_ITEM_KEYS,
  STATUS_KEYS,
  copyBlockedKey,
  countByStatus,
  effectiveMode,
  needsSafetyChecklist,
  openSteps,
  pickText,
  readSteps,
} from "./setup-logic";
import { getSetupGuide } from "./wizard-api";
import type { SetupStatus, SetupStep } from "./wizard-types";

const CHECKED_KEY = "levi-automatic-setup-checked";

const STATUS_TONE: Record<
  SetupStatus,
  "success" | "warning" | "info" | "neutral"
> = { ok: "success", warn: "warning", todo: "info", unknown: "neutral" };
const STATUS_ICON = {
  ok: CircleCheck,
  warn: TriangleAlert,
  todo: ListTodo,
  unknown: CircleHelp,
} as const;

function loadChecked(): Set<string> {
  try {
    const raw = readBrowserStorage("session", CHECKED_KEY);
    const list = raw ? (JSON.parse(raw) as unknown) : [];
    return new Set(
      Array.isArray(list) ? list.filter((x) => typeof x === "string") : [],
    );
  } catch {
    return new Set();
  }
}

/** An executor runs one `execute` step; it throws with a sentence on failure.
 * No executor for a step means the step is offered as a command to copy. */
export type SetupExecutors = Record<string, () => Promise<void>>;

export function SetupGuide({
  steps,
  executors,
}: {
  steps: SetupStep[];
  executors?: SetupExecutors;
}) {
  const { t, language } = useLocale();
  const [safety, setSafety] = useState<boolean[]>(() =>
    SAFETY_ITEM_KEYS.map(() => false),
  );
  const [checked, setChecked] = useState<Set<string>>(() => new Set());
  const [running, setRunning] = useState<string | null>(null);
  const [failure, setFailure] = useState<{
    id: string;
    message: string;
  } | null>(null);
  useEffect(() => setChecked(loadChecked()), []);

  const safetyDone = safety.every(Boolean);
  const counts = useMemo(() => countByStatus(steps), [steps]);
  const open = openSteps(steps, checked);
  const anyMoves = steps.some(needsSafetyChecklist);

  const toggleChecked = (id: string, on: boolean) =>
    setChecked((prev) => {
      const next = new Set(prev);
      if (on) next.add(id);
      else next.delete(id);
      writeBrowserStorage("session", CHECKED_KEY, JSON.stringify([...next]));
      return next;
    });

  const run = async (step: SetupStep) => {
    const executor = executors?.[step.id];
    if (!executor || running) return;
    setRunning(step.id);
    setFailure(null);
    try {
      await executor();
    } catch (error) {
      setFailure({
        id: step.id,
        message: error instanceof Error ? error.message : String(error),
      });
    } finally {
      setRunning(null);
    }
  };

  return (
    <div className="aw-setup">
      <Note tone="warning">
        <strong>{t("automatic.setup.onsite.title")}</strong>{" "}
        {t("automatic.setup.onsite.body")}
      </Note>
      <p className="aw-setup__summary" role="status">
        {t("automatic.setup.summary")
          .replace("{ok}", String(counts.ok))
          .replace("{total}", String(steps.length))
          .replace("{open}", String(open))}
      </p>

      {anyMoves && (
        <fieldset className="aw-safety">
          <legend>
            <Icon icon={OctagonAlert} /> {t("automatic.setup.safety.title")}
          </legend>
          <p className="aw-safety__warn" role="note">
            {t("automatic.setup.safety.notestop")}
          </p>
          {SAFETY_ITEM_KEYS.map((key, index) => (
            <Checkbox
              key={key}
              label={t(key)}
              checked={safety[index]}
              onChange={(event) =>
                setSafety((prev) =>
                  prev.map((v, i) => (i === index ? event.target.checked : v)),
                )
              }
            />
          ))}
        </fieldset>
      )}

      {steps.length === 0 ? (
        <p className="pg-pool-hint">{t("automatic.setup.empty")}</p>
      ) : (
        <ol className="aw-steps">
          {steps.map((step) => {
            const mode = effectiveMode(step, Boolean(executors?.[step.id]));
            const blocked = copyBlockedKey(step, safetyDone);
            const title = pickText(step.title, language);
            const why = pickText(step.why, language);
            const mine = checked.has(step.id);
            return (
              <li
                key={step.id}
                className="aw-step"
                data-status={step.status}
                data-level={step.level}
              >
                <div className="aw-step__head">
                  <Badge
                    tone={STATUS_TONE[step.status]}
                    icon={STATUS_ICON[step.status]}
                  >
                    {t(STATUS_KEYS[step.status])}
                  </Badge>
                  <strong className="aw-step__title">{title}</strong>
                  <span className="aw-step__level">
                    {t(LEVEL_KEYS[step.level])}
                  </span>
                </div>
                {why && <p className="aw-step__why">{why}</p>}
                {step.detail && (
                  <p className="aw-step__detail">
                    <code>{step.detail}</code>
                  </p>
                )}
                {mode === "native" && (
                  <p className="aw-step__mode">
                    {t("automatic.setup.mode.native")}
                  </p>
                )}
                {mode === "execute" && (
                  <div className="pg-row">
                    <Button
                      size="sm"
                      variant="secondary"
                      loading={running === step.id}
                      disabled={running !== null && running !== step.id}
                      onClick={() => void run(step)}
                    >
                      {t("automatic.setup.run")}
                    </Button>
                    <span className="aw-step__mode">
                      {t("automatic.setup.mode.execute")}
                    </span>
                  </div>
                )}
                {mode === "copy" && (
                  <>
                    <p className="aw-step__mode">
                      {step.mode === "execute"
                        ? t("automatic.setup.mode.copy_fallback")
                        : t("automatic.setup.mode.copy")}
                    </p>
                    {blocked ? (
                      <p className="aw-step__blocked" role="note">
                        <Icon icon={OctagonAlert} /> {t(blocked)}
                      </p>
                    ) : (
                      step.command && (
                        <SetupCommand command={step.command} label={title} />
                      )
                    )}
                  </>
                )}
                {failure?.id === step.id && (
                  <RequestProblem
                    action="automatic.setup.run_failed"
                    message={failure.message}
                  />
                )}
                {step.status !== "ok" && (
                  <Checkbox
                    label={t("automatic.setup.checked")}
                    description={t("automatic.setup.checked_note")}
                    checked={mine}
                    onChange={(event) =>
                      toggleChecked(step.id, event.target.checked)
                    }
                  />
                )}
              </li>
            );
          })}
        </ol>
      )}
    </div>
  );
}

/** The guide read from the server: loading, the steps, or a problem with a
 * Try again button. */
export function SetupGuideLoader({
  load = getSetupGuide,
  executors,
  onLoaded,
}: {
  load?: () => Promise<unknown>;
  executors?: SetupExecutors;
  onLoaded?: (steps: SetupStep[]) => void;
}) {
  const { t } = useLocale();
  const [state, setState] = useState<
    | { kind: "loading" }
    | { kind: "error"; message: string }
    | { kind: "ready"; steps: SetupStep[] }
  >({ kind: "loading" });
  const fetchSteps = useCallback(async () => {
    setState({ kind: "loading" });
    try {
      const steps = readSteps(await load());
      setState({ kind: "ready", steps });
      onLoaded?.(steps);
    } catch (error) {
      setState({
        kind: "error",
        message: error instanceof Error ? error.message : String(error),
      });
    }
  }, [load, onLoaded]);
  useEffect(() => {
    void fetchSteps();
    // The loader reads once on mount; "Try again" reads again.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  if (state.kind === "loading")
    return (
      <div role="status" aria-label={t("automatic.setup.loading")}>
        <Skeleton height={64} />
      </div>
    );
  if (state.kind === "error")
    return (
      <RequestProblem
        action="automatic.setup.load_failed"
        message={state.message}
        onRetry={() => void fetchSteps()}
      />
    );
  return <SetupGuide steps={state.steps} executors={executors} />;
}
