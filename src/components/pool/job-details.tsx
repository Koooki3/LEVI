"use client";
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { ClipboardCopy, X } from "lucide-react";
import { useLocale } from "@/components/levi-locale";
import { leviRequest } from "@/components/levi-api";
import { Button, IconButton } from "@/components/ds";
import { Problem, RequestProblem } from "@/components/pages-ui/feedback";
import { useServerText } from "@/components/pages-ui/messages";
import { bytes, StatusBadge } from "./pool-progress";
import type { JobDetails, JobWarning, PoolJob } from "./types";

/** Why an episode was left out by a data check: the server names the check
 * (levi/pool/jobs.py ``REASONS``), the page says what it means. */
const REASONS: Record<string, string> = {
  stale_state:
    "The robot state clock stopped updating (the readings were frozen)",
  stall_markers: "The collector recorded a camera stall (frozen frames)",
  csv_schema: "A data file of the capture is empty or malformed",
  cameras: "A camera video of the capture is missing or unreadable",
  frame_count: "A camera's frame count does not match the state rows",
  fps: "The frame rates of the capture cannot be used for this export",
  metadata_frames:
    "metadata.json is unreadable or its frame count does not match",
  other: "Another data check (the message says which)",
};

/** Warnings that carry a code but no sentence of their own. */
const WARNING_TITLES: Record<string, string> = {
  copy_task_conflict:
    "Copies of one recording have different task texts; the original's text is used",
};

/** The last two folders of an episode path: task and episode. */
export function shortEpisode(path: string): string {
  const parts = path.split("/").filter(Boolean);
  return parts.slice(-2).join("/") || path;
}

/** "and 28 more" / "还有 28 个". */
function useMore() {
  const { t } = useLocale();
  return (n: number) => t("and {count} more").replace("{count}", String(n));
}

function Warning({ item }: { item: JobWarning }) {
  const { t } = useLocale();
  const more = useMore();
  const serverText = useServerText();
  const title =
    item.message ||
    (item.code && WARNING_TITLES[item.code]
      ? t(WARNING_TITLES[item.code])
      : item.code) ||
    "";
  const total = item.episodes_total ?? item.episodes?.length ?? 0;
  const shown = item.episodes ?? [];
  return (
    <li>
      <p>
        {serverText(title)}
        {item.code && (
          <small className="pg-pool-muted">
            {" "}
            · <code>{item.code}</code>
            {item.count ? ` · ${item.count}` : ""}
          </small>
        )}
      </p>
      {shown.length > 0 && (
        <details>
          <summary>
            {total} {t("episodes")}
          </summary>
          <ul className="pg-pool-detail-list">
            {shown.map((e) => (
              <li key={e}>
                <code title={e}>{shortEpisode(e)}</code>
              </li>
            ))}
            {total > shown.length && (
              <li className="pg-pool-muted">{more(total - shown.length)}</li>
            )}
          </ul>
        </details>
      )}
    </li>
  );
}

/** What a job did beyond its status: the numbers, every warning, the
 * episodes a data check left out (grouped by reason) and the ones that
 * failed with an exception, with the traceback. */
export function JobDetailsDialog({
  job,
  onClose,
}: {
  job: PoolJob | null;
  onClose: () => void;
}) {
  const { t } = useLocale();
  const more = useMore();
  const serverText = useServerText();
  const titleId = useId();
  const ref = useRef<HTMLDialogElement>(null);
  const [data, setData] = useState<JobDetails | null>(null);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);
  const id = job?.id;
  // The job the dialog is about now: an answer for an earlier one is dropped.
  const current = useRef<string | undefined>(undefined);
  const load = useCallback(async () => {
    if (!id) return;
    current.current = id;
    try {
      const value = await leviRequest<JobDetails>(
        "GET",
        `pool/jobs/${encodeURIComponent(id)}/details`,
      );
      if (current.current !== id) return;
      setData(value);
      setError("");
    } catch (e) {
      if (current.current !== id) return;
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [id]);
  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (id) {
      if (!dialog.open) dialog.showModal();
      setData(null);
      setError("");
      setCopied(false);
      void load();
    } else {
      current.current = undefined;
      if (dialog.open) dialog.close();
    }
  }, [id, load]);

  const left = data?.left_out;
  const failed = data?.failed;
  const nothing =
    !!data &&
    !data.warnings.length &&
    !left?.total &&
    !failed?.total &&
    !data.error &&
    !data.error_info;
  return (
    <dialog
      ref={ref}
      className="pg-pool-dialog pg-pool-logdialog ds-on-raised"
      aria-labelledby={titleId}
      onClose={onClose}
    >
      <form method="dialog" className="pg-pool-dialog-head">
        <h2 id={titleId}>
          {t("Job details")} <code>{id}</code>
        </h2>
        <IconButton
          type="submit"
          icon={X}
          label={t("Close")}
          tooltipPlacement="bottom"
        />
      </form>
      {error && (
        <RequestProblem
          action="The job details could not be read"
          message={error}
          onRetry={() => void load()}
        />
      )}
      {!data && !error && <p className="pg-pool-muted">{t("Checking…")}</p>}
      {data && (
        <div className="pg-pool-details" tabIndex={0}>
          <p>
            <StatusBadge status={data.status} />
            {data.summary.episodes !== undefined && (
              <span className="pg-pool-muted">
                {" "}
                · {data.summary.episodes} {t("episodes")}
                {data.summary.frames !== undefined &&
                  ` · ${data.summary.frames} ${t("frames")}`}
                {data.summary.bytes ? ` · ${bytes(data.summary.bytes)}` : ""}
              </span>
            )}
          </p>
          {(data.error || data.error_info) && (
            <Problem
              tone="danger"
              title={t("The job failed")}
              why={serverText(data.error_info?.message || data.error || "")}
              fix={
                data.error_info?.hint
                  ? serverText(data.error_info.hint)
                  : undefined
              }
            />
          )}
          {data.warnings.length > 0 && (
            <section>
              <h3>
                {t("Warnings")} ({data.warnings.length})
              </h3>
              <ul className="pg-pool-detail-list">
                {data.warnings.map((w, i) => (
                  <Warning key={`${w.code ?? "w"}-${i}`} item={w} />
                ))}
              </ul>
            </section>
          )}
          {!!left?.total && (
            <section>
              <h3>
                {t("Episodes left out by data checks")} ({left.total})
              </h3>
              <p className="pg-pool-hint">
                {t(
                  "These episodes are not in the export; the export itself is complete.",
                )}{" "}
                {t("An episode can fail more than one check.")}
              </p>
              <ul className="pg-pool-detail-list">
                {left.groups.map((g) => (
                  <li key={g.code}>
                    <strong>{g.count}</strong> ×{" "}
                    {t(REASONS[g.code] ?? REASONS.other)}
                  </li>
                ))}
              </ul>
              <details>
                <summary>{t("Every episode and its message")}</summary>
                <ul className="pg-pool-detail-list">
                  {left.items.map((item, i) => (
                    <li key={`${item.episode}-${i}`}>
                      <code title={item.episode}>
                        {shortEpisode(item.episode)}
                      </code>{" "}
                      <span className="pg-pool-muted">
                        {serverText(item.message)}
                      </span>
                    </li>
                  ))}
                  {left.total > left.items.length && (
                    <li className="pg-pool-muted">
                      {more(left.total - left.items.length)}
                    </li>
                  )}
                </ul>
              </details>
            </section>
          )}
          {!!failed?.total && (
            <section>
              <h3>
                {t("Episodes that failed to convert")} ({failed.total})
              </h3>
              <ul className="pg-pool-detail-list">
                {failed.items.map((item, i) => (
                  <li key={`${item.episode}-${i}`}>
                    <code title={item.episode}>
                      {shortEpisode(item.episode)}
                    </code>{" "}
                    <strong>{item.type}</strong>: {serverText(item.message)}
                    {item.traceback && (
                      <details>
                        <summary>{t("Traceback")}</summary>
                        <pre className="pg-pool-log" tabIndex={0}>
                          {item.traceback}
                        </pre>
                      </details>
                    )}
                  </li>
                ))}
                {failed.total > failed.items.length && (
                  <li className="pg-pool-muted">
                    {more(failed.total - failed.items.length)}
                  </li>
                )}
              </ul>
            </section>
          )}
          {nothing && (
            <p className="pg-pool-muted">
              {t("Nothing to report: no warnings and no errors.")}
            </p>
          )}
        </div>
      )}
      <div className="pg-row">
        <Button
          icon={ClipboardCopy}
          disabled={!data}
          onClick={() => {
            void navigator.clipboard
              ?.writeText(JSON.stringify(data, null, 2))
              .then(() => setCopied(true));
          }}
        >
          {copied ? t("Copied") : t("Copy details")}
        </Button>
      </div>
    </dialog>
  );
}
