// The labelling queue in words: the status file's per-dataset rows and its
// yield rules (`levi/live/queue.py`, `levi/live/gpumgr.py`). Pure; a code the
// page does not know shows the server's own text, so nothing is lost.
import { clock } from "./live-logic";
import type { QueueRow, YieldInfo } from "./types";

type T = (key: string) => string;

const YIELD_REASON: Record<string, string> = {
  deployment:
    "A policy server or an evaluation session is running on the robot side.",
  robot_quiet: "The robot is moving, so labelling stays out of the way.",
  foreign_gpu: "Another program is using the GPU.",
  foreign_lock: "Another program holds the GPU lock.",
  probe_error:
    "The GPU or the port list could not be read, so labelling assumes the GPU is in use.",
  resuming: "Every reason has gone; labelling is about to resume.",
};

const YIELD_RECOVERY: Record<string, string> = {
  deployment: "Resumes after the policy server stops and the session ends.",
  robot_quiet: "Resumes when the robot is no longer moving.",
  foreign_gpu: "Resumes when the other program frees the GPU.",
  foreign_lock: "Resumes when the other program lets go of the lock.",
  probe_error: "Resumes when the GPU and the ports can be read again.",
  resuming: "",
};

/** What a paused row says, by the service's sentence. */
const PAUSED_REASON: Record<string, string> = {
  "paused by a person; the queue is kept":
    "Paused by a person; the waiting episodes are kept.",
  "labelling is off for this dataset":
    "Labelling is switched off for this dataset.",
  "queue.json cannot be read":
    "The queue file cannot be read, so labelling is held.",
};

export const QUEUE_STATE_LABEL: Record<string, string> = {
  queued: "Queued",
  running: "Labelling now",
  yielding: "Yielding the GPU",
  paused: "Paused",
  done: "Up to date",
  failed: "Failing, will retry",
};

export const QUEUE_MODE_LABEL: Record<string, string> = {
  auto: "Automatic",
  history: "History only",
  paused: "Paused",
  off: "Off",
};

const RANK: Record<string, number> = {
  failed: 0,
  running: 1,
  yielding: 2,
  queued: 3,
  paused: 4,
  done: 5,
};

export type YieldReason = { code?: string; text?: string };

export function yieldReasonText(reason: YieldReason, t: T): string {
  const known = reason.code ? YIELD_REASON[reason.code] : undefined;
  return known ? t(known) : (reason.text ?? reason.code ?? "");
}

export function yieldRecoveryText(reason: YieldReason, t: T): string {
  const known = reason.code ? YIELD_RECOVERY[reason.code] : undefined;
  return known ? t(known) : "";
}

/** The rows that are usable, most urgent first (a failing dataset, then the
 * one being labelled, then those held back, then the finished). */
export function queueRows(rows: unknown): QueueRow[] {
  if (!Array.isArray(rows)) return [];
  return rows
    .filter(
      (row): row is QueueRow =>
        Boolean(row) &&
        typeof row === "object" &&
        typeof (row as QueueRow).dataset === "string" &&
        (row as QueueRow).dataset !== "",
    )
    .map((row, index) => ({ row, index }))
    .sort(
      (a, b) =>
        (RANK[a.row.state ?? ""] ?? 6) - (RANK[b.row.state ?? ""] ?? 6) ||
        a.index - b.index,
    )
    .map(({ row }) => row);
}

/** Why a dataset is not being labelled right now, in words. */
export function queueRowReason(
  row: QueueRow,
  yieldInfo: YieldInfo | null | undefined,
  t: T,
): string {
  if (row.state === "yielding") {
    const reasons = yieldInfo?.reasons ?? [];
    if (reasons.length)
      return reasons.map((r) => yieldReasonText(r, t)).join(" ");
    return t("Labelling is yielding the GPU.");
  }
  const text = row.reason ?? "";
  if (row.state === "paused" && PAUSED_REASON[text])
    return t(PAUSED_REASON[text]);
  return text;
}

/** When labelling is expected to carry on after a yield. */
export function resumeText(
  yieldInfo: YieldInfo | null | undefined,
  t: T,
): string {
  if (!yieldInfo?.active) return "";
  if (typeof yieldInfo.resume_at === "number")
    return t(
      "Labelling is expected to resume at {time} if no reason comes back.",
    ).replace("{time}", clock(yieldInfo.resume_at));
  return t(
    "Labelling resumes by itself once every reason has gone and stayed gone for the configured wait.",
  );
}
