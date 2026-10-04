"use client";
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { useLocale } from "@/components/levi-locale";
import { leviRequest } from "@/components/levi-api";
import {
  CircleAlert,
  ClipboardCopy,
  FileText,
  Play,
  RefreshCw,
  RotateCcw,
  TriangleAlert,
  X,
} from "lucide-react";
import { Button, Icon, IconButton, Tooltip } from "@/components/ds";
import { RequestProblem } from "@/components/pages-ui/feedback";
import { ago, pollDelay } from "./pool-progress";
import type { PoolJob } from "./types";

/** Ask for the job again while it lives, at the pace its state calls for:
 * a dead worker is noticed within one poll and the banner replaces the bar. */
export function useJobPolling(
  job: PoolJob | null,
  onJob: (job: PoolJob) => void,
) {
  const id = job?.id;
  const status = job?.status ?? "";
  const delay = pollDelay(status);
  useEffect(() => {
    if (!id || delay === null) return;
    let live = true;
    const timer = setTimeout(() => {
      leviRequest<PoolJob>("GET", `pool/jobs/${encodeURIComponent(id)}`)
        .then((next) => live && onJob(next))
        .catch(() => {});
    }, delay);
    return () => {
      live = false;
      clearTimeout(timer);
    };
    // The job object changes on every poll; the effect re-arms with it.
  }, [job, id, delay, onJob]);
}

/** Resume / re-run / cancel / log / error report of one job. */
export function useJobActions(onJob: (job: PoolJob) => void) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const call = useCallback(
    async (job: PoolJob, action: "resume" | "rerun" | "cancel") => {
      setBusy(true);
      setError("");
      try {
        const next = await leviRequest<PoolJob>(
          "POST",
          `pool/jobs/${encodeURIComponent(job.id)}/${action}`,
        );
        onJob(next);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setBusy(false);
      }
    },
    [onJob],
  );
  return { busy, error, setError, call };
}

/** The tail of a job's timestamped log, refreshable and copyable. */
export function LogDialog({
  job,
  onClose,
}: {
  job: PoolJob | null;
  onClose: () => void;
}) {
  const { t } = useLocale();
  const titleId = useId();
  const ref = useRef<HTMLDialogElement>(null);
  const [text, setText] = useState("");
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);
  const id = job?.id;
  const load = useCallback(async () => {
    if (!id) return;
    try {
      const value = await leviRequest<{ text: string }>(
        "GET",
        `pool/jobs/${encodeURIComponent(id)}/log?kb=64`,
      );
      setText(value.text);
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [id]);
  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (id && !dialog.open) {
      dialog.showModal();
      setText("");
      void load();
    } else if (!id && dialog.open) dialog.close();
  }, [id, load]);
  return (
    <dialog
      ref={ref}
      className="pg-pool-dialog pg-pool-logdialog ds-on-raised"
      aria-labelledby={titleId}
      onClose={onClose}
    >
      <form method="dialog" className="pg-pool-dialog-head">
        <h2 id={titleId}>
          {t("Job log")} <code>{id}</code>
        </h2>
        <IconButton type="submit" icon={X} label={t("Close")} />
      </form>
      {error && (
        <RequestProblem
          action="The log could not be read"
          message={error}
          onRetry={() => void load()}
        />
      )}
      <pre className="pg-pool-log" tabIndex={0}>
        {text || t("The log is empty.")}
      </pre>
      <div className="pg-row">
        <Button icon={RefreshCw} onClick={() => void load()}>
          {t("Refresh")}
        </Button>
        <Button
          icon={ClipboardCopy}
          onClick={() => {
            void navigator.clipboard
              ?.writeText(text)
              .then(() => setCopied(true));
          }}
        >
          {copied ? t("Copied") : t("Copy")}
        </Button>
      </div>
    </dialog>
  );
}

const TITLES: Record<string, string> = {
  interrupted: "Interrupted",
  failed: "Failed",
  stalled: "Stalled",
  done_with_errors: "Finished with errors",
};

/** The prominent note for a job that stopped, failed, went quiet or left
 * episodes out: why, what to do, and the buttons for it. */
export function JobBanner({
  job,
  onJob,
  onLog,
}: {
  job: PoolJob;
  onJob: (job: PoolJob) => void;
  onLog: (job: PoolJob) => void;
}) {
  const { t } = useLocale();
  const { busy, error, setError, call } = useJobActions(onJob);
  const [copied, setCopied] = useState(false);
  const status = job.status;
  const title = TITLES[status];
  if (!title) return null;
  const info = job.error_info;
  const live = status === "stalled";
  const tone = status === "done_with_errors" || live ? "warn" : "fail";
  const detail =
    status === "done_with_errors"
      ? `${job.result?.errors ?? job.failures ?? 0} ${t("episode(s) were left out; see the log and errors.jsonl.")}`
      : live
        ? t(
            "The worker is alive but nothing has moved. Wait for it, or cancel the job.",
          )
        : info?.message || job.error || "";
  async function copyReport() {
    try {
      const report = await leviRequest<unknown>(
        "GET",
        `pool/jobs/${encodeURIComponent(job.id)}/error-report`,
      );
      await navigator.clipboard.writeText(JSON.stringify(report, null, 2));
      setCopied(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }
  return (
    <div className={`pg-pool-banner ${tone}`} role="alert">
      <p className="pg-pool-banner-title">
        <Icon icon={tone === "warn" ? TriangleAlert : CircleAlert} />
        <strong>{t(title)}</strong>
        {job.reason && status === "interrupted" && (
          <span> · {t(job.reason)}</span>
        )}
        {job.age_seconds !== undefined && job.age_seconds !== null && (
          <span className="pg-pool-muted">
            {" "}
            · {t("Last update")} {ago(job.age_seconds, t)}
          </span>
        )}
      </p>
      {detail && <p>{t(detail)}</p>}
      {info?.hint && status !== "done_with_errors" && (
        <p className="pg-pool-hint">{t(info.hint)}</p>
      )}
      {job.partial && job.resumable && (
        <p className="pg-pool-hint">
          {t("Unfinished output kept in")} <code>{job.partial}</code>
        </p>
      )}
      {error && (
        <RequestProblem action="The action did not complete" message={error} />
      )}
      <div className="pg-row">
        {job.resumable && (
          <Button
            variant="primary"
            icon={Play}
            disabled={busy}
            onClick={() => void call(job, "resume")}
          >
            {t("Resume")}
          </Button>
        )}
        {job.rerunnable && status !== "done_with_errors" && (
          <Tooltip
            content={t(
              "Plan again from the saved recipe; unfinished output goes",
            )}
          >
            <Button
              variant={error && !job.resumable ? "primary" : "secondary"}
              icon={RotateCcw}
              disabled={busy}
              onClick={() => void call(job, "rerun")}
            >
              {t("Re-run")}
            </Button>
          </Tooltip>
        )}
        {status !== "done_with_errors" && (
          <Tooltip
            content={t("Stop for good and remove the unfinished output")}
          >
            <Button
              icon={X}
              disabled={busy}
              onClick={() => void call(job, "cancel")}
            >
              {t("Cancel")}
            </Button>
          </Tooltip>
        )}
        <Button variant="ghost" icon={FileText} onClick={() => onLog(job)}>
          {t("View log")}
        </Button>
        <Button
          variant="ghost"
          icon={ClipboardCopy}
          onClick={() => void copyReport()}
        >
          {copied ? t("Copied") : t("Copy error report")}
        </Button>
      </div>
    </div>
  );
}
