"use client";
import Link from "next/link";
import { useState } from "react";
import { useLocale } from "@/components/levi-locale";
import { ago } from "@/components/pool/pool-progress";
import {
  clock,
  filterReviewRuns,
  reviewRuns,
  segmentSummary,
  verdictTally,
  type ReviewFilter,
} from "./live-logic";
import { Chip, type Tone } from "./session-panels";
import type { DatasetDetail, DatasetRow, DemoRow } from "./types";
import type { DetailEntry } from "./use-live";

const STATE_LABELS: Record<string, [string, Tone]> = {
  idle: ["Idle", ""],
  pending: ["Waiting to be labelled", ""],
  annotating: ["Being labelled", "pass"],
  awaiting_approval: ["Awaiting a person's approval", "warn"],
  error: ["Error, retrying later", "fail"],
};

const DEMO_STATES: Record<string, string> = {
  mirrored: "waiting",
  annotating: "labelling",
  done: "done",
  failed: "failed",
  rejected: "rejected",
  skipped_human: "skipped (a person annotated it)",
};

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div>
      <strong>{value}</strong>
      <span>{label}</span>
    </div>
  );
}

/** The automatic verdict, drawn so it cannot be mistaken for a gold label:
 * dashed outline, "auto", and the words "not reviewed". */
function AutoOutcome({ detail }: { detail: DatasetDetail | undefined }) {
  const { t } = useLocale();
  const tally = verdictTally(detail?.demos);
  const judged = tally.success + tally.failure + tally.undecided;
  return (
    <div className="levi-live-auto">
      <div className="levi-live-auto-head">
        <span className="levi-live-auto-tag">{t("auto")}</span>
        <strong>{t("Automatic outcome")}</strong>
        <span className="levi-pool-muted">
          {t("not reviewed, accuracy not evaluated")}
        </span>
      </div>
      {!detail ? (
        <span className="levi-pool-muted">{t("Loading…")}</span>
      ) : judged === 0 ? (
        <span className="levi-pool-muted">
          {t("No automatic verdict yet.")}
        </span>
      ) : (
        <div className="levi-live-chips">
          <span className="levi-live-autochip">
            {t("success")} {tally.success}
          </span>
          <span className="levi-live-autochip">
            {t("failure")} {tally.failure}
          </span>
          {tally.undecided > 0 && (
            <span className="levi-live-autochip">
              {t("undecided")} {tally.undecided}
            </span>
          )}
          {tally.none > 0 && (
            <span className="levi-live-autochip dim">
              {t("no verdict")} {tally.none}
            </span>
          )}
        </div>
      )}
    </div>
  );
}

function ReviewRuns({
  detail,
  filter,
  onFilter,
  nowSeconds,
}: {
  detail: DatasetDetail | undefined;
  filter: ReviewFilter;
  onFilter: (value: ReviewFilter) => void;
  nowSeconds: number;
}) {
  const { t } = useLocale();
  const runs = reviewRuns(detail);
  if (runs.length === 0) return null;
  const { shown, hidden } = filterReviewRuns(runs, filter, nowSeconds);
  return (
    <div className="levi-live-reviews">
      <div className="levi-live-reviews-head">
        <strong>
          {t("Review runs left for a person")} ({runs.length})
        </strong>
        <label className="levi-pool-muted">
          {t("Show")}{" "}
          <select
            className="levi-input"
            value={filter}
            onChange={(e) => onFilter(e.target.value as ReviewFilter)}
          >
            <option value="latest">{t("the newest 3")}</option>
            <option value="day">{t("the last 24 hours")}</option>
            <option value="all">{t("all")}</option>
          </select>
        </label>
      </div>
      <p className="levi-pool-muted">
        {t(
          "One per batch. The service never commits them: a person accepts or rejects the outcome proposals in LEVI. This filter only hides rows on this page.",
        )}
      </p>
      <ul>
        {shown.map((r) => (
          <li key={r.runId}>
            <code>{r.runId}</code>
            <span>
              {r.demos} {t("episodes")} · {t("success")} {r.success} ·{" "}
              {t("failure")} {r.failure}
              {r.undecided > 0 && ` · ${t("undecided")} ${r.undecided}`}
            </span>
            <span className="levi-pool-muted">{r.at ? clock(r.at) : "—"}</span>
          </li>
        ))}
      </ul>
      {hidden > 0 && (
        <p className="levi-pool-muted">
          {hidden} {t("older run(s) hidden")}
        </p>
      )}
    </div>
  );
}

function DemoList({ demos }: { demos: DemoRow[] }) {
  const { t } = useLocale();
  const [all, setAll] = useState(false);
  const shown = all ? demos : demos.slice(0, 10);
  if (demos.length === 0) return null;
  return (
    <div className="levi-live-demos">
      <strong>{t("Episodes (newest first)")}</strong>
      <ul>
        {shown.map((d) => (
          <li key={d.demo}>
            <code>{d.demo}</code>
            <span>{t(DEMO_STATES[d.state ?? ""] ?? d.state ?? "—")}</span>
            <span>
              {d.segments != null ? `${d.segments} ${t("time segments")}` : "—"}
            </span>
            <span className="levi-live-autocell">
              {d.verdict ? (
                <>
                  <span className="levi-live-auto-tag">{t("auto")}</span>{" "}
                  {d.verdict.undecided || !d.verdict.outcome
                    ? t("undecided")
                    : t(d.verdict.outcome)}
                  {d.verdict.events != null &&
                    ` (${d.verdict.valid_events ?? 0}/${d.verdict.events})`}
                </>
              ) : d.reason ? (
                <span className="levi-pool-muted">{d.reason}</span>
              ) : (
                "—"
              )}
            </span>
          </li>
        ))}
      </ul>
      {demos.length > 10 && (
        <button
          type="button"
          className="levi-pool-link"
          onClick={() => setAll((v) => !v)}
        >
          {all ? t("Show fewer") : `${t("Show all")} ${demos.length}`}
        </button>
      )}
    </div>
  );
}

export function DatasetCard({
  name,
  row,
  entry,
  fault,
  open,
  onToggle,
  workerPhase,
  filter,
  onFilter,
  nowSeconds,
}: {
  name: string;
  row: DatasetRow;
  entry: DetailEntry | undefined;
  fault: "current" | "earlier" | null;
  open: boolean;
  onToggle: () => void;
  workerPhase: string | null;
  filter: ReviewFilter;
  onFilter: (value: ReviewFilter) => void;
  nowSeconds: number;
}) {
  const { t } = useLocale();
  const detail = entry?.data ?? undefined;
  const [stateLabel, stateTone] = STATE_LABELS[row.state ?? ""] ?? [
    row.state ?? "",
    "",
  ];
  const seg = segmentSummary(detail?.demos);
  const fraction = row.episodes ? Math.min(1, row.done / row.episodes) : 0;
  const error = row.last_error || detail?.last_error || "";
  const extras: [string, number | undefined][] = [
    ["still being written", row.waiting],
    ["skipped", row.skipped],
    ["rejected (unusable video)", row.rejected],
    ["older than the service (not touched)", row.backlog],
    ["aborted or incomplete", row.incomplete],
    ["discarded", row.discarded],
  ];
  const shownExtras = extras.filter(([, n]) => (n ?? 0) > 0);
  const title = detail?.group
    ? `${detail.group} / ${detail.task_folder}`
    : name.replace("__", " / ");
  return (
    <article
      className={`levi-live-card${fault === "current" ? " fault" : ""}`}
      aria-label={title}
    >
      <header>
        <h3>
          <code>{title}</code>
        </h3>
        <div className="levi-live-chips">
          <Chip tone={stateTone}>{t(stateLabel)}</Chip>
          {fault === "current" && <Chip tone="fail">{t("FR3 fault")}</Chip>}
          {fault === "earlier" && (
            <Chip
              tone="warn"
              title={(row.fault_reasons ?? []).join("; ") || undefined}
            >
              {row.fr3_fault
                ? `${t("Earlier FR3 fault")}: ${row.fr3_fault} ${t("aborted")}`
                : t("Earlier fault")}
            </Chip>
          )}
        </div>
      </header>
      {detail?.task_text && (
        <p className="levi-live-prompt">“{detail.task_text}”</p>
      )}

      <div className="levi-live-stats">
        <Stat label={t("mirrored")} value={row.episodes} />
        <Stat label={t("waiting")} value={row.pending} />
        <Stat label={t("labelling")} value={row.annotating} />
        <Stat label={t("done")} value={row.done} />
        <Stat label={t("failed")} value={row.failed} />
      </div>
      <div
        className="levi-bar levi-live-bar"
        role="progressbar"
        aria-label={t("Episodes labelled")}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(fraction * 100)}
      >
        <span style={{ width: `${(fraction * 100).toFixed(1)}%` }} />
      </div>
      {shownExtras.length > 0 && (
        <p className="levi-pool-muted">
          {shownExtras.map(([label, n]) => `${n} ${t(label)}`).join(" · ")}
        </p>
      )}

      <p className="levi-live-line">
        <strong>{t("Time segments")}:</strong>{" "}
        {detail
          ? seg.committed > 0
            ? `${seg.segments} ${t("segments in")} ${seg.committed} ${t("episodes")}`
            : t("none committed yet")
          : "—"}
        {detail?.current?.demos?.length
          ? ` · ${t("batch in progress")}: ${detail.current.demos.length} ${t("episodes")}${workerPhase ? ` (${workerPhase})` : ""}`
          : ""}
      </p>
      <AutoOutcome detail={detail} />
      {error && (
        <p className="levi-error">
          <strong>{t("Last error")}:</strong> {error}
        </p>
      )}
      <ReviewRuns
        detail={detail}
        filter={filter}
        onFilter={onFilter}
        nowSeconds={nowSeconds}
      />
      <p className="levi-pool-muted">
        {t("Last processed")}:{" "}
        {row.last_processed_at
          ? ago(nowSeconds - row.last_processed_at, t)
          : t("never")}
      </p>
      <div className="levi-row levi-live-actions">
        {detail?.repo_id ? (
          <Link className="levi-secondary" href={`/${detail.repo_id}`}>
            {t("Open in the viewer")}
          </Link>
        ) : (
          <span className="levi-pool-muted">
            {detail
              ? t("Not registered in LEVI yet: no viewer link.")
              : t("Loading…")}
          </span>
        )}
        <Link className="levi-pool-link" href="/workbench">
          {t("Conversion & review")}
        </Link>
        <button
          type="button"
          className="levi-pool-link"
          aria-expanded={open}
          onClick={onToggle}
        >
          {open ? t("Hide episodes") : t("Show episodes")}
        </button>
      </div>
      {open &&
        (entry?.error && !detail ? (
          <p className="levi-error">{entry.error}</p>
        ) : (
          <DemoList demos={detail?.demos ?? []} />
        ))}
    </article>
  );
}
