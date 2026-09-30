"use client";
import { useLocale } from "@/components/levi-locale";
import { ago } from "@/components/pool/pool-progress";
import { clock, isLost, shortDuration, type FaultInfo } from "./live-logic";
import type { Fr3Health, LiveSession } from "./types";

export type Tone = "pass" | "warn" | "fail" | "";

export function Chip({
  tone = "",
  children,
  title,
}: {
  tone?: Tone;
  children: React.ReactNode;
  title?: string;
}) {
  return (
    <span className={`levi-status levi-live-chip ${tone}`} title={title}>
      {children}
    </span>
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
  return <Chip tone={tone}>{t(label)}</Chip>;
}

/** The red banner: the user asked to see an FR3 fault at once. */
export function FaultBanner({ fault }: { fault: FaultInfo }) {
  const { t } = useLocale();
  if (!fault.active) return null;
  return (
    <section className="levi-live-banner" role="alert" aria-live="assertive">
      <strong>{t("FR3 fault detected: the evaluation was interrupted")}</strong>
      <p>
        {fault.redLight
          ? t("The FR3 health monitor reports the red light.")
          : t("An evaluation session stopped because of a fault.")}{" "}
        {t(
          "The client does not move the robot after a fault. Clear the fault, then start the client again: it continues the run.",
        )}
      </p>
      {fault.sessions.length > 0 && (
        <ul>
          {fault.sessions.map((s) => (
            <li key={`${s.group}/${s.task_folder}`}>
              <code>
                {s.group} / {s.task_folder}
              </code>
            </li>
          ))}
        </ul>
      )}
      {fault.reasons.length > 0 && (
        <ul className="levi-live-reasons">
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
    <article className={`levi-live-card${fault ? " fault" : ""}`}>
      <header>
        <h3>
          <code>
            {s.group} / {s.task_folder}
          </code>
        </h3>
        <div className="levi-live-chips">
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
      {s.prompt && <p className="levi-live-prompt">“{s.prompt}”</p>}
      {fault && (
        <p className="levi-error">
          {s.reason || t("The session reports a fault.")}
        </p>
      )}
      <dl className="levi-live-dl">
        <Field label={t("Run")}>
          {s.run_id ? <code>{s.run_id}</code> : t("manual session (no run)")}
        </Field>
        <Field label={t("Episode")}>
          {ep.no != null ? (
            <>
              {ep.no}
              {ep.target ? ` / ${ep.target}` : ""}
              {ep.counted != null && (
                <span className="levi-pool-muted">
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
              <span>
                {ep.step ?? 0} / {ep.max_steps}
              </span>
              <div
                className="levi-bar levi-live-bar"
                role="progressbar"
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={Math.round((steps ?? 0) * 100)}
              >
                <span
                  style={{
                    width: `${(Math.min(1, steps ?? 0) * 100).toFixed(1)}%`,
                  }}
                />
              </div>
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
                <span className="levi-pool-muted">
                  {" "}
                  · {clock(last.ended_at)}
                </span>
              )}
            </>
          ) : (
            "—"
          )}
        </Field>
        <Field label={t("Reset wait")}>
          {s.reset_wait_s != null ? `${s.reset_wait_s} s` : "—"}
        </Field>
        <Field label={t("Robot (client's view)")}>
          {inband.ok === false || inbandProblems.length > 0 ? (
            <span className="levi-live-bad">
              {inbandProblems.join("; ") || t("not OK")}
            </span>
          ) : inband.ok === true ? (
            <>
              {inband.robot_mode_name || t("OK")}
              {inband.source && (
                <span className="levi-pool-muted"> · {inband.source}</span>
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
  return (
    <section className="levi-live-section" aria-labelledby="live-sessions">
      <h2 id="live-sessions">
        {t("Evaluation sessions")}{" "}
        <span className="levi-pool-muted">({sessions.length})</span>
      </h2>
      {sessions.length === 0 ? (
        <p className="levi-pool-hint">
          {t(
            "No evaluation session is reporting. The evaluation client writes one status file per model and task folder while it runs.",
          )}
        </p>
      ) : (
        <div className="levi-live-cards">
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
    <span className="levi-live-bad">{t("No")}</span>
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
      className={`levi-live-section${state === "red" ? " fault" : ""}`}
      aria-labelledby="live-fr3"
    >
      <h2 id="live-fr3">{t("FR3 robot arm")}</h2>
      <div className="levi-live-chips">
        <Chip tone={tone}>{t(label)}</Chip>
        {fr3?.age_s != null && state !== "missing" && (
          <span className="levi-pool-muted">
            {t("Updated")} {ago(fr3.age_s, t)}
          </span>
        )}
      </div>
      <p className="levi-pool-hint">{t(note)}</p>
      {state && state !== "missing" && (
        <dl className="levi-live-dl">
          <Field label={t("Robot mode")}>
            {fr3?.robot_mode_name || "—"}
            {fr3?.robot_mode != null && (
              <span className="levi-pool-muted"> ({fr3.robot_mode})</span>
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
              <span className="levi-live-bad">
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
          <h3 className="levi-live-sub">{t("Reasons")}</h3>
          <ul className="levi-live-reasons">
            {fr3.reasons.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
