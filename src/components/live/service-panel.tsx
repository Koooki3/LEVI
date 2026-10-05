"use client";
import { useEffect, useRef, useState } from "react";
import { useLocale } from "@/components/levi-locale";
import { ago } from "@/components/pool/pool-progress";
import {
  blockedRunsSummary,
  clock,
  explainGate,
  gpuModeNote,
  serviceStateNote,
  shortDuration,
  vllmLabel,
} from "./live-logic";
import { Chip, Field, type Tone } from "./session-panels";
import { Check, ClipboardCopy, Hand, PlugZap, Server } from "lucide-react";
import { Button, Icon } from "@/components/ds";
import { EmptyLine, Problem, withCode } from "@/components/pages-ui/feedback";
import type { LiveStatusResponse } from "./types";
import { PAUSE_NOTES, type NeedsPerson } from "./live-logic";

export const START_COMMAND = "levi live start";
export const RESUME_COMMAND = "levi live resume";

/** A command in a box with a Copy button. */
export function CopyCommand({ command }: { command: string }) {
  const { t } = useLocale();
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);
  const copy = () => {
    void navigator.clipboard
      ?.writeText(command)
      .then(() => {
        setCopied(true);
        timer.current = setTimeout(() => setCopied(false), 2000);
      })
      .catch(() => {});
  };
  return (
    <div className="pg-row">
      <code className="pg-live-command">{command}</code>
      <Button size="sm" icon={copied ? Check : ClipboardCopy} onClick={copy}>
        {copied ? t("Copied") : t("Copy")}
      </Button>
    </div>
  );
}

/** Runs the live gate stopped (a person's runs started in the LEVI page):
 * the ones that go on by themselves are told as a quiet line, the ones that
 * need a press on Resume get the attention colour. */
export function BlockedRunsNote({
  status,
}: {
  status: LiveStatusResponse | null;
}) {
  const { t } = useLocale();
  const { waiting, needsPerson } = blockedRunsSummary(status);
  if (!waiting && !needsPerson) return null;
  const say = (text: string, n: number) => t(text).replace("{n}", String(n));
  return (
    <section
      className={needsPerson ? "pg-live-attention" : "pg-live-blocked"}
      role="status"
      aria-live="polite"
    >
      {needsPerson > 0 && (
        <p>
          {say(
            "{n} run(s) stopped for the robot's policy need you to press Resume in the LEVI page.",
            needsPerson,
          )}
        </p>
      )}
      {waiting > 0 && (
        <p className="pg-pool-hint">
          {say(
            "{n} run(s) stopped for the robot's policy will continue by themselves once the evaluation lets the model work again.",
            waiting,
          )}
        </p>
      )}
    </section>
  );
}

/** What a person has to do: resume the service, approve a plan or commit a
 * draft in the LEVI page, or look at a page that did not start. */
export function AttentionBanner({ need }: { need: NeedsPerson }) {
  const { t } = useLocale();
  if (
    !need.paused &&
    !need.attention &&
    !need.awaiting.length &&
    !need.frontendFailed &&
    need.loopStalledS == null
  )
    return null;
  const note = need.paused ? PAUSE_NOTES[need.paused.code ?? ""] : undefined;
  return (
    <section className="pg-live-attention" role="alert" aria-live="polite">
      <strong>
        <Icon icon={Hand} />
        {t("The live service needs a person")}
      </strong>
      {need.paused && (
        <>
          <p>
            <strong className="pg-live-inline">
              {t("The service paused labelling")}
            </strong>
            {note ? `: ${t(note.title)}. ` : ". "}
            {t(
              "The evaluation is not affected and no episode is lost: they are collected and labelled after labelling resumes.",
            )}
          </p>
          {note && (
            <p>
              {t(note.detail)} {t(note.todo)}
            </p>
          )}
          {need.paused.reason && (
            <p className="pg-live-reasons-line">
              <span className="pg-pool-muted">{t("Reason")}:</span>{" "}
              <code>{need.paused.reason}</code>
            </p>
          )}
          {note?.command && <CopyCommand command={note.command} />}
        </>
      )}
      {need.loopStalledS != null && (
        <p>
          {t("The service's main loop may be stuck")}:{" "}
          {Math.round(need.loopStalledS / 60)} {t("min since its last tick")}.{" "}
          {t(
            "The status still updates, but nothing new is started. Run `levi live doctor`; if it stays stuck, `levi live stop` and start it again.",
          )}
        </p>
      )}
      {need.attention && (
        <>
          <p>
            {t(
              "Labelling is paused: the model server failed to start several times in a row. Rollouts are still being collected and will be labelled after you resume.",
            )}
          </p>
          {need.attention.reason && (
            <p className="pg-live-reasons-line">
              <span className="pg-pool-muted">{t("Reason")}:</span>{" "}
              <code>{need.attention.reason}</code>
            </p>
          )}
          <CopyCommand command={RESUME_COMMAND} />
        </>
      )}
      {need.awaiting.map((a) => (
        <p key={a.name}>
          <code>{a.name.replace("__", " / ")}</code>:{" "}
          {a.kind === "changes"
            ? t(
                "waiting for you to commit the draft in the LEVI page (Agent Workbench). Labelling of this dataset continues after that.",
              )
            : t(
                "waiting for you to approve the plan in the LEVI page (Agent Workbench). Labelling of this dataset continues after that.",
              )}
        </p>
      ))}
      {need.frontendFailed && (
        <p>
          {t(
            "The page or API the service starts did not come up. Labelling is not affected.",
          )}{" "}
          {need.frontendFailed.error && (
            <code>{need.frontendFailed.error}</code>
          )}
        </p>
      )}
    </section>
  );
}

/** Shown instead of the service's numbers when it is not running. */
export function ServiceOffline({
  status,
  coreError,
}: {
  status: LiveStatusResponse | null;
  coreError: string;
}) {
  const { t } = useLocale();
  const last = status?.service ? status.age_s : null;
  return (
    <section className="pg-live-offline" role="status">
      <strong>
        <Icon icon={PlugZap} />
        {coreError
          ? t("The LEVI core is not answering")
          : t("The background service is not running")}
      </strong>
      <p>
        {coreError
          ? withCode(
              t(
                "This page cannot reach the live LEVI core, so nothing below is current. Check that `levi live start` is running and that this page belongs to it.",
              ),
            )
          : t(
              "Finished rollouts are not being labelled. Evaluation sessions and the FR3 state below are still read straight from their files.",
            )}
      </p>
      {last != null && (
        <p className="pg-pool-muted">
          {t("Last heard from it")}: {ago(last, t)}
        </p>
      )}
      <CopyCommand command={START_COMMAND} />
      <p className="pg-pool-muted">
        {t(
          "Add --daemon to keep it running in the background, and --auto-approve to let it approve its own plans.",
        )}
      </p>
    </section>
  );
}

const SERVICE_TONES: Record<string, Tone> = {
  annotating: "pass",
  active: "pass",
  idle: "",
  starting: "warn",
  gpu_wait: "warn",
  error: "fail",
  stopped: "warn",
};
const SERVICE_LABELS: Record<string, string> = {
  starting: "Starting",
  idle: "Idle",
  active: "Active",
  annotating: "Annotating",
  gpu_wait: "Waiting for the GPU",
  error: "Error",
  stopped: "Stopped",
};

export function ServicePanel({
  status,
  alive,
  now,
}: {
  status: LiveStatusResponse | null;
  alive: boolean;
  now: number;
}) {
  const { t } = useLocale();
  const service = status?.service;
  if (!service) {
    return (
      <section className="pg-live-section" aria-labelledby="live-service">
        <h2 id="live-service">{t("Service and resources")}</h2>
        <EmptyLine icon={Server}>{t("No status file yet.")}</EmptyLine>
      </section>
    );
  }
  const gpu = service.gpu ?? {};
  const gate = explainGate(service);
  const vllm = gpu.vllm_state ?? gpu.vllm?.state;
  const res = service.resources ?? {};
  const state = service.state ?? "";
  const vllmTone: Tone =
    vllm === "ready" ? "pass" : vllm === "error" ? "fail" : vllm ? "" : "";
  return (
    // A service that is not running is said in words ("Not running", the note
    // below), not by fading the section: opacity would take the text under
    // the contrast the rest of the page keeps.
    <section
      className="pg-live-section"
      data-alive={alive ? "true" : "false"}
      aria-labelledby="live-service"
    >
      <h2 id="live-service">{t("Service and resources")}</h2>
      <div className="pg-live-chips">
        {alive ? (
          <Chip tone={SERVICE_TONES[state] ?? ""}>
            {t(SERVICE_LABELS[state] ?? state)}
          </Chip>
        ) : (
          <Chip tone="warn">{t("Not running")}</Chip>
        )}
        {service.frontend?.state === "starting" && (
          <Chip tone="warn">{t("Page starting")}</Chip>
        )}
        {service.frontend?.state === "failed" && (
          <Chip tone="fail">{t("Page or API did not start")}</Chip>
        )}
        {service.auto_approve ? (
          <Chip title={t("The audited automatic approver is on")}>
            {t("Automatic approver on")}
          </Chip>
        ) : (
          <Chip title={t("A person approves each plan in the LEVI page")}>
            {t("Automatic approver off")}
          </Chip>
        )}
      </div>
      {!alive && (
        <p className="pg-pool-muted">
          {t("Everything below is the last known state.")}
        </p>
      )}
      {alive && serviceStateNote(state) && (
        <p className="pg-pool-hint">{t(serviceStateNote(state))}</p>
      )}
      {service.last_error && (
        <Problem
          live={false}
          title={t("Last error")}
          why={service.last_error}
        />
      )}

      <h3 className="pg-live-sub">{t("GPU")}</h3>
      <dl className="pg-live-dl">
        <Field label={t("Mode")}>
          <code>{gpu.mode ?? "—"}</code>
        </Field>
        <Field label={t("Model server (vLLM)")}>
          <Chip tone={vllmTone}>{t(vllmLabel(vllm))}</Chip>
          {gpu.vllm?.owned === false && vllm && vllm !== "stopped" && (
            <span className="pg-pool-muted">
              {" "}
              {t("started by someone else")}
            </span>
          )}
        </Field>
        <Field label={t("Labelling gate")}>
          <Chip tone={gpu.gate?.open === false ? "warn" : "pass"}>
            {gpu.gate?.open === false ? t("Closed") : t("Open")}
          </Chip>
        </Field>
        <Field label={t("Policy server")}>
          {gpu.policy_server_seen == null
            ? "—"
            : gpu.policy_server_seen
              ? t("listening")
              : t("not seen")}
        </Field>
        <Field label={t("Free GPU memory")}>
          {gpu.free_mib != null
            ? `${(gpu.free_mib / 1024).toFixed(1)} GiB`
            : "—"}
        </Field>
      </dl>
      {alive && gate && (
        <div className={`pg-live-gate ${gate.tone}`}>
          <strong>{t(gate.title)}</strong>
          <p>{t(gate.detail)}</p>
        </div>
      )}
      {alive && gpuModeNote(gpu.mode) && (
        <p className="pg-pool-muted">{t(gpuModeNote(gpu.mode))}</p>
      )}

      <h3 className="pg-live-sub">{t("Work")}</h3>
      <dl className="pg-live-dl">
        <Field label={t("Queue")}>
          {service.queue_depth ?? 0} {t("dataset(s) waiting")}
        </Field>
        <Field label={t("Worker")}>
          {service.worker ? (
            <>
              <code>{service.worker.dataset ?? "—"}</code>
              {service.worker.phase && (
                <span className="pg-pool-muted"> · {service.worker.phase}</span>
              )}
            </>
          ) : (
            t("none")
          )}
        </Field>
        <Field label={t("Watching")}>
          {service.watch_roots?.length
            ? service.watch_roots.map((r) => (
                <code key={r} className="pg-live-path">
                  {r}
                </code>
              ))
            : "—"}
        </Field>
      </dl>

      <h3 className="pg-live-sub">{t("Resources (supervisor)")}</h3>
      <dl className="pg-live-dl">
        <Field label={t("Memory")}>
          {res.rss_mb != null ? `${res.rss_mb.toFixed(0)} MiB` : "—"}
        </Field>
        <Field label={t("Threads")}>{res.threads ?? "—"}</Field>
        <Field label="CPU">
          {res.cpu_percent != null ? `${res.cpu_percent.toFixed(1)} %` : "—"}
        </Field>
        <Field label={t("Running for")}>
          {service.started_at
            ? shortDuration(now / 1000 - service.started_at)
            : "—"}
        </Field>
      </dl>

      {service.events && service.events.length > 0 && (
        <details className="pg-live-events">
          <summary>
            {t("Recent events")} ({service.events.length})
          </summary>
          <ul>
            {service.events.slice(0, 12).map((e, i) => (
              <li key={`${e.time}-${i}`} className={e.level}>
                <span className="pg-pool-muted">{clock(e.time)}</span> {e.text}
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}
