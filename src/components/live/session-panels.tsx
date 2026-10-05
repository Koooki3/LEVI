"use client";
import { useEffect, useState } from "react";
import { useLocale } from "@/components/levi-locale";
import { ago } from "@/components/pool/pool-progress";
import {
  clock,
  isLost,
  resetRemaining,
  shortDuration,
  sortSessions,
  type FaultInfo,
} from "./live-logic";
import { Bot, OctagonAlert } from "lucide-react";
import { EmptyLine, Problem } from "@/components/pages-ui/feedback";
import {
  Badge,
  Icon,
  Progress,
  StatusDot,
  Tooltip,
  type Tone as DsTone,
} from "@/components/ds";
import type { Fr3Health, LiveSession } from "./types";

export type Tone = "pass" | "warn" | "fail" | "";

const CHIP_TONE: Record<Tone, DsTone> = {
  pass: "success",
  warn: "warning",
  fail: "danger",
  "": "neutral",
};

/** A state as a badge (icon shape + colour + words). `live` marks a running
 * state with the breathing dot; `title` becomes a tooltip (hover and focus). */
export function Chip({
  tone = "",
  children,
  title,
  live = false,
}: {
  tone?: Tone;
  children: React.ReactNode;
  title?: string;
  live?: boolean;
}) {
  const chip = live ? (
    <StatusDot tone={CHIP_TONE[tone]} live>
      {children}
    </StatusDot>
  ) : (
    <Badge tone={CHIP_TONE[tone]}>{children}</Badge>
  );
  if (!title) return chip;
  return (
    <Tooltip content={title}>
      <span tabIndex={0} className="pg-badge-trigger">
        {chip}
      </span>
    </Tooltip>
  );
}

const SESSION_STATES: Record<string, [string, Tone]> = {
  standby: ["Standby", ""],
  homing: ["Returning home", ""],
  running: ["Running", "pass"],
  waiting_reset: ["Waiting for reset", ""],
  fault: ["Fault", "fail"],
  stopped: ["Stopped", "warn"],
  finished: ["Finished", "pass"],
  crashed: ["Crashed", "fail"],
};

export function SessionState({ state }: { state: string }) {
  const { t } = useLocale();
  const [label, tone] = SESSION_STATES[state] ?? [state, ""];
  return (
    <Chip tone={tone} live={state === "running"}>
      {t(label)}
    </Chip>
  );
}

/** The red banner: the user asked to see an FR3 fault at once. */
export function FaultBanner({ fault }: { fault: FaultInfo }) {
  const { t } = useLocale();
  if (!fault.active) return null;
  return (
    <section className="pg-live-banner" role="alert" aria-live="assertive">
      <strong>
        <Icon icon={OctagonAlert} size="md" />
        {t("FR3 fault detected: the evaluation was interrupted")}
      </strong>
      <p>
        {fault.redLight
          ? t("The FR3 health monitor reports the red light.")
          : t("An evaluation session stopped because of a fault.")}{" "}
        {t(
          "The client does not move the robot after a fault. Clear the fault, then start the client again: it continues the run.",
        )}
      </p>
      {fault.sessions.length > 0 && (
        <p>
          {t("Session in fault")}:{" "}
          {fault.sessions.map((s, i) => (
            <span key={`${s.group}/${s.task_folder}`}>
              {i > 0 && ", "}
              <code>
                {s.group} / {s.task_folder}
              </code>
            </span>
          ))}
        </p>
      )}
      {fault.reasons.length > 0 && (
        <ul className="pg-live-reasons">
          {fault.reasons.map((r) => (
            <li key={r}>{r}</li>
          ))}
        </ul>
      )}
    </section>
  );
}

export function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </>
  );
}

function outcomeLabel(outcome: string | null | undefined): string {
  switch (outcome) {
    case "unlabeled":
      return "Waiting for LEVI's verdict";
    case "aborted":
      return "Aborted";
    case "success":
      return "Success";
    case "failure":
      return "Failure";
    default:
      return outcome || "—";
  }
}

/** The reset wait, counting down in the browser between polls. */
function ResetWait({ session }: { session: LiveSession }) {
  const { t } = useLocale();
  const [now, setNow] = useState(() => Date.now());
  const counting = session.state === "waiting_reset";
  useEffect(() => {
    if (!counting) return;
    const timer = setInterval(() => {
      if (document.visibilityState !== "hidden") setNow(Date.now());
    }, 500);
    return () => clearInterval(timer);
  }, [counting]);
  const wait = session.reset_wait_s;
  const left = resetRemaining(session, now / 1000);
  if (wait == null) return <>—</>;
  if (left == null) return <>{wait} s</>;
  if (left <= 0)
    return (
      <>
        {wait} s ·{" "}
        <span className="pg-live-bad">
          {t("the next episode should have started")}
        </span>
      </>
    );
  return (
    <>
      {wait} s · {t("next episode in")} <strong>{Math.ceil(left)} s</strong>
    </>
  );
}

function SessionCard({ session: s }: { session: LiveSession }) {
  const { t } = useLocale();
  const ep = s.episode ?? {};
  const last = s.last_episode ?? {};
  const lost = isLost(s);
  const fault = s.state === "fault" || s.fault;
  const steps = ep.max_steps ? (ep.step ?? 0) / ep.max_steps : null;
  const inband = s.fr3 ?? {};
  const inbandProblems = [
    ...(inband.errors ?? []),
    ...(inband.reasons ?? []),
  ].slice(0, 4);
  return (
    <article className={`pg-live-card${fault ? " fault" : ""}`}>
      <header>
        <h3>
          <code>
            {s.group} / {s.task_folder}
          </code>
        </h3>
        <div className="pg-live-chips">
          <SessionState state={s.state} />
          {lost && (
            <Chip tone="warn" title={t("No heartbeat from the client")}>
              {t("Lost contact")} · {ago(s.age_s, t)}
            </Chip>
          )}
          {s.levi_enabled === true && <Chip>{t("LEVI labelling on")}</Chip>}
          {s.levi_enabled === false && <Chip>{t("Manual labelling")}</Chip>}
        </div>
      </header>
      {s.prompt && <p className="pg-live-prompt">“{s.prompt}”</p>}
      {fault && (
        <Problem
          live={false}
          title={t("Fault")}
          why={s.reason || t("The session reports a fault.")}
        />
      )}
      <dl className="pg-live-dl">
        <Field label={t("Run")}>
          {s.run_id ? <code>{s.run_id}</code> : t("manual session (no run)")}
        </Field>
        <Field label={t("Episode")}>
          {ep.no != null ? (
            <>
              {ep.no}
              {ep.target ? ` / ${ep.target}` : ""}
              {ep.counted != null && (
                <span className="pg-pool-muted">
                  {" "}
                  · {t("counted")} {ep.counted}
                </span>
              )}
            </>
          ) : (
            "—"
          )}
        </Field>
        <Field label={t("Steps")}>
          {ep.max_steps ? (
            <>
              <Progress
                className="pg-live-bar"
                value={Math.min(1, steps ?? 0) * 100}
                label={`${ep.step ?? 0} / ${ep.max_steps}`}
              />
            </>
          ) : (
            "—"
          )}
        </Field>
        <Field label={t("Policy")}>
          {s.policy?.config || s.policy?.checkpoint ? (
            <>
              <code>{s.policy?.config || "—"}</code>
              <br />
              <code>{s.policy?.checkpoint || "—"}</code>
            </>
          ) : (
            "—"
          )}
        </Field>
        <Field label={t("Most recent episode")}>
          {last.outcome || last.steps != null ? (
            <>
              {t(outcomeLabel(last.outcome))}
              {last.steps != null && ` · ${last.steps} ${t("steps")}`}
              {last.duration_s != null &&
                ` · ${shortDuration(last.duration_s)}`}
              {last.ended_at != null && (
                <span className="pg-pool-muted"> · {clock(last.ended_at)}</span>
              )}
            </>
          ) : (
            "—"
          )}
        </Field>
        <Field label={t("Reset wait")}>
          <ResetWait session={s} />
        </Field>
        <Field label={t("Robot (client's view)")}>
          {inband.ok === false || inbandProblems.length > 0 ? (
            <span className="pg-live-bad">
              {inbandProblems.join("; ") || t("not OK")}
            </span>
          ) : inband.ok === true ? (
            <>
              {inband.robot_mode_name || t("OK")}
              {inband.source && (
                <span className="pg-pool-muted"> · {inband.source}</span>
              )}
            </>
          ) : (
            "—"
          )}
        </Field>
      </dl>
    </article>
  );
}

export function SessionsPanel({ sessions }: { sessions: LiveSession[] }) {
  const { t } = useLocale();
  sessions = sortSessions(sessions);
  return (
    <section className="pg-live-section" aria-labelledby="live-sessions">
      <h2 id="live-sessions">
        {t("Evaluation sessions")}{" "}
        <span className="pg-pool-muted">({sessions.length})</span>
      </h2>
      {sessions.length === 0 ? (
        <EmptyLine icon={Bot}>
          {t(
            "No evaluation session is reporting. The evaluation client writes one status file per model and task folder while it runs.",
          )}
        </EmptyLine>
      ) : (
        <div className="pg-live-cards">
          {sessions.map((s) => (
            <SessionCard key={`${s.group}/${s.task_folder}`} session={s} />
          ))}
        </div>
      )}
    </section>
  );
}

function YesNo({ value }: { value: boolean | null | undefined }) {
  const { t } = useLocale();
  if (value == null) return <>—</>;
  return value ? (
    <>{t("Yes")}</>
  ) : (
    <span className="pg-live-bad">{t("No")}</span>
  );
}

export function Fr3Panel({ fr3 }: { fr3: Fr3Health | null | undefined }) {
  const { t } = useLocale();
  const state = fr3?.state;
  const [label, tone, note]: [string, Tone, string] =
    state === "red"
      ? [
          "Red light",
          "fail",
          "The robot reports an error or a stopped controller.",
        ]
      : state === "ok"
        ? ["OK", "pass", "No error reported by the robot."]
        : state === "offline"
          ? [
              "Monitor offline",
              "warn",
              "The health monitor stopped updating. This is not a red light: the page cannot tell. The evaluation client still watches the robot on its own (see each session).",
            ]
          : [
              "Monitor not running",
              "warn",
              "There is no health file. Start the FR3 health monitor in the ROS terminal to see the robot's state here.",
            ];
  return (
    <section
      className={`pg-live-section${state === "red" ? " fault" : ""}`}
      aria-labelledby="live-fr3"
    >
      <h2 id="live-fr3">{t("FR3 robot arm")}</h2>
      <div className="pg-live-chips">
        <Chip tone={tone}>{t(label)}</Chip>
        {fr3?.age_s != null && state !== "missing" && (
          <span className="pg-pool-muted">
            {t("Updated")} {ago(fr3.age_s, t)}
          </span>
        )}
      </div>
      <p className="pg-pool-hint">{t(note)}</p>
      {state && state !== "missing" && (
        <dl className="pg-live-dl">
          <Field label={t("Robot mode")}>
            {fr3?.robot_mode_name || "—"}
            {fr3?.robot_mode != null && (
              <span className="pg-pool-muted"> ({fr3.robot_mode})</span>
            )}
          </Field>
          <Field label={t("Hardware active")}>
            <YesNo value={fr3?.hardware_active} />
          </Field>
          <Field label={t("Controller active")}>
            <YesNo value={fr3?.controller_active} />
          </Field>
          <Field label={t("Current errors")}>
            {fr3?.current_errors?.length ? (
              <span className="pg-live-bad">
                {fr3.current_errors.join(", ")}
              </span>
            ) : (
              t("none")
            )}
          </Field>
          {fr3?.last_motion_errors && fr3.last_motion_errors.length > 0 && (
            <Field label={t("Last motion errors")}>
              {fr3.last_motion_errors.join(", ")}
            </Field>
          )}
          {fr3?.command_success_rate != null && (
            <Field label={t("Command success rate")}>
              {(fr3.command_success_rate * 100).toFixed(0)}%
            </Field>
          )}
        </dl>
      )}
      {fr3?.reasons && fr3.reasons.length > 0 && (
        <>
          <h3 className="pg-live-sub">{t("Reasons")}</h3>
          <ul className="pg-live-reasons">
            {fr3.reasons.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
