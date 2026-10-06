"use client";
import Link from "next/link";
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
import { datasetLinks, type LiveLink } from "./embedding";
import { EpisodeList } from "./episode-list";
import { Chip, type Tone } from "./session-panels";
import { ArrowUpRight, ChevronDown, ChevronUp } from "lucide-react";
import { Button, Icon, Progress, Skeleton } from "@/components/ds";
import { Problem, RequestProblem } from "@/components/pages-ui/feedback";
import { count, percent } from "./stats-logic";
import type {
  AgreementShare,
  AgreementSummary,
  DatasetDetail,
  DatasetRow,
} from "./types";
import type { DetailEntry } from "./use-live";

const STATE_LABELS: Record<string, [string, Tone]> = {
  idle: ["Idle", ""],
  pending: ["Waiting to be labelled", ""],
  annotating: ["Being labelled", "pass"],
  awaiting_approval: ["Awaiting a person's approval", "warn"],
  error: ["Error, retrying later", "fail"],
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
    <div className="pg-live-auto">
      <div className="pg-live-auto-head">
        <span className="pg-live-auto-tag">{t("auto")}</span>
        <strong>{t("Automatic outcome")}</strong>
        <span className="pg-pool-muted">
          {t("not reviewed, accuracy not evaluated")}
        </span>
      </div>
      {!detail ? (
        <span className="pg-live-loading" aria-busy="true">
          <span className="sr-only">{t("Loading…")}</span>
          <Skeleton width="60%" height={20} />
        </span>
      ) : judged === 0 ? (
        <span className="pg-pool-muted">{t("No automatic verdict yet.")}</span>
      ) : (
        <div className="pg-live-chips">
          <span className="pg-live-autochip">
            {t("success")} {tally.success}
          </span>
          <span className="pg-live-autochip">
            {t("failure")} {tally.failure}
          </span>
          {tally.undecided > 0 && (
            <span className="pg-live-autochip">
              {t("undecided")} {tally.undecided}
            </span>
          )}
          {tally.none > 0 && (
            <span className="pg-live-autochip dim">
              {t("no verdict")} {tally.none}
            </span>
          )}
        </div>
      )}
    </div>
  );
}

const share = (found: AgreementShare | undefined) =>
  `${count(found?.n)}/${count(found?.of)}`;

/** The agent label (automatic, unreviewed) against the operator label (ground
 * truth) over every episode of the dataset; nothing until an episode has
 * both. */
export function AgentVsOperator({
  agreement,
}: {
  agreement: AgreementSummary | undefined;
}) {
  const { t } = useLocale();
  if (!agreement?.pairs) return null;
  const a = agreement;
  const parts = [
    `${t("both labels")} ${count(a.pairs)}`,
    `${t("agree")} ${count(a.agree)}/${count(a.judged)} (${percent(a.rate)})`,
    `${t("false success")} ${share(a.false_success)}`,
    `${t("missed success")} ${share(a.missed_success)}`,
    `${t("agent undecided")} ${count(a.undecided)}`,
    `${t("no agent verdict yet")} ${count(a.no_agent)}`,
    `${t("success rate: operator / agent")} ${percent(a.operator_success_rate)} / ${percent(a.agent_success_rate)}`,
  ];
  return (
    <div className="pg-live-auto pg-live-dual">
      <div className="pg-live-auto-head">
        <span className="pg-live-optag">{t("operator")}</span>
        <strong>{t("Agent vs operator")}</strong>
        <span className="pg-pool-muted">
          {t("Operator label (ground truth)")} ·{" "}
          {t("agent label (automatic, unreviewed)")}
        </span>
      </div>
      <p className="pg-live-line">{parts.join(" · ")}</p>
    </div>
  );
}

function ReviewRuns({
  detail,
  openCount,
  filter,
  onFilter,
  nowSeconds,
}: {
  detail: DatasetDetail | undefined;
  openCount: number;
  filter: ReviewFilter;
  onFilter: (value: ReviewFilter) => void;
  nowSeconds: number;
}) {
  const { t } = useLocale();
  const runs = reviewRuns(detail);
  if (runs.length === 0 && openCount === 0) return null;
  const { shown, hidden } = filterReviewRuns(runs, filter, nowSeconds);
  return (
    <div className="pg-live-reviews">
      <div className="pg-live-reviews-head">
        <strong>
          {t("Review runs left for a person")} ({openCount || runs.length})
        </strong>
        <label className="pg-pool-muted">
          {t("Show")}{" "}
          <select
            className="ds-input ds-focus"
            value={filter}
            onChange={(e) => onFilter(e.target.value as ReviewFilter)}
          >
            <option value="latest">{t("the newest 3")}</option>
            <option value="day">{t("the last 24 hours")}</option>
            <option value="all">{t("all")}</option>
          </select>
        </label>
      </div>
      <p className="pg-pool-muted">
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
            <span className="pg-pool-muted">{r.at ? clock(r.at) : "—"}</span>
          </li>
        ))}
      </ul>
      {hidden > 0 && (
        <p className="pg-pool-muted">
          {hidden} {t("older run(s) hidden")}
        </p>
      )}
    </div>
  );
}

/** A link here, or to the live workspace's own page in a new tab. */
function LinkTo({
  link,
  className,
  children,
}: {
  link: LiveLink;
  className: string;
  children: React.ReactNode;
}) {
  if (link.external)
    return (
      <a
        className={className}
        href={link.href}
        target="_blank"
        rel="noopener noreferrer"
      >
        {children}
      </a>
    );
  return (
    <Link className={className} href={link.href}>
      {children}
    </Link>
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
  onChanged,
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
  /** An episode was removed or restored: fetch this card's detail again. */
  onChanged: () => void;
}) {
  const { t } = useLocale();
  const detail = entry?.data ?? undefined;
  const links = datasetLinks(detail);
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
    ["removed by a person (not counted)", row.excluded],
  ];
  const shownExtras = extras.filter(([, n]) => (n ?? 0) > 0);
  const title = detail?.group
    ? `${detail.group} / ${detail.task_folder}`
    : name.replace("__", " / ");
  return (
    <article
      className={`pg-live-card${fault === "current" ? " fault" : ""}`}
      aria-label={title}
    >
      <header>
        <h3>
          <code>{title}</code>
        </h3>
        <div className="pg-live-chips">
          <Chip tone={stateTone}>{t(stateLabel)}</Chip>
          {row.state === "awaiting_approval" && (
            <Chip tone="warn">
              {row.awaiting === "changes"
                ? t("Waiting for you: commit the draft")
                : t("Waiting for you: approve the plan")}
            </Chip>
          )}
          {(row.stuck ?? 0) > 0 && (
            <Chip
              tone="warn"
              title={t(
                "Never finished and unchanged for a while: counted, no longer waited for.",
              )}
            >
              {row.stuck} {t("stuck")}
            </Chip>
          )}
          {(row.source_changed ?? 0) > 0 && (
            <Chip
              tone="warn"
              title={t(
                "The source was replaced after it was mirrored; an episode that was already labelled keeps the old content.",
              )}
            >
              {row.source_changed} {t("source replaced")}
            </Chip>
          )}
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
        <p className="pg-live-prompt">“{detail.task_text}”</p>
      )}

      <div className="pg-live-stats">
        <Stat label={t("mirrored")} value={row.episodes} />
        <Stat label={t("waiting")} value={row.pending} />
        <Stat label={t("labelling")} value={row.annotating} />
        <Stat label={t("done")} value={row.done} />
        <Stat label={t("failed")} value={row.failed} />
      </div>
      <Progress
        className="pg-live-bar"
        value={fraction * 100}
        showValue
        label={t("Episodes labelled")}
      />
      {shownExtras.length > 0 && (
        <p className="pg-pool-muted">
          {shownExtras.map(([label, n]) => `${n} ${t(label)}`).join(" · ")}
        </p>
      )}

      <p className="pg-live-line">
        <strong>{t("Time segments")}:</strong>{" "}
        {detail
          ? detail.pipeline?.temporal === false
            ? t("off: release review only")
            : seg.committed > 0
              ? `${seg.segments} ${t("segments in")} ${seg.committed} ${t("episodes")}`
              : t("none committed yet")
          : "—"}
        {detail?.current?.demos?.length
          ? ` · ${t("batch in progress")}: ${detail.current.demos.length} ${t("episodes")}${workerPhase ? ` (${workerPhase})` : ""}`
          : ""}
      </p>
      <AutoOutcome detail={detail} />
      <AgentVsOperator agreement={detail?.agreement} />
      {row.state === "awaiting_approval" && (
        <p className="pg-live-await">
          {row.awaiting === "changes"
            ? t(
                "The service finished the temporary work and waits for you to commit the draft in the LEVI page (Agent Workbench); nothing is written until you do.",
              )
            : t(
                "The service made a plan and waits for you to approve it in the LEVI page (Agent Workbench); it does nothing on this dataset until you do.",
              )}
          {detail?.embedded &&
            ` ${t(
              detail.live_ui
                ? "That is the live workspace's own page (Conversion & review below), not this LEVI."
                : "That is the live workspace's own page, not this LEVI: start the service with `levi live start --ui` to open it, or with --auto-approve to let it approve its own plans.",
            )}`}
        </p>
      )}
      {error && (
        <Problem
          live={false}
          title={t("Last error")}
          why={error}
          fix={t(
            "The service retries by itself; nothing to do unless it repeats.",
          )}
        />
      )}
      <ReviewRuns
        detail={detail}
        openCount={row.review_runs_open ?? 0}
        filter={filter}
        onFilter={onFilter}
        nowSeconds={nowSeconds}
      />
      <p className="pg-pool-muted">
        {t("Last processed")}:{" "}
        {row.last_processed_at
          ? ago(nowSeconds - row.last_processed_at, t)
          : t("never")}
      </p>
      <div className="pg-row pg-live-actions">
        {links.viewer ? (
          <LinkTo
            className="ds-btn ds-btn--secondary ds-focus"
            link={links.viewer}
          >
            {t("Open in the viewer")}
            <Icon icon={ArrowUpRight} />
          </LinkTo>
        ) : (
          <span className="pg-pool-muted">
            {!detail
              ? t("Loading…")
              : detail.embedded
                ? t(
                    "The episodes' viewer and their review are on the live workspace's own page: start the service with `levi live start --ui` to open them.",
                  )
                : t("Not registered in LEVI yet: no viewer link.")}
          </span>
        )}
        {links.review && (
          <LinkTo
            className="ds-btn ds-btn--ghost ds-btn--sm ds-focus"
            link={links.review}
          >
            {t("Conversion & review")}
          </LinkTo>
        )}
        <Button
          size="sm"
          variant="ghost"
          iconEnd={open ? ChevronUp : ChevronDown}
          aria-expanded={open}
          onClick={onToggle}
        >
          {open ? t("Hide episodes") : t("Show episodes")}
        </Button>
      </div>
      {open &&
        (entry?.error && !detail ? (
          <RequestProblem
            action="The episodes could not be loaded"
            message={entry.error}
            onRetry={onChanged}
          />
        ) : (
          <EpisodeList dataset={name} detail={detail} onChanged={onChanged} />
        ))}
    </article>
  );
}
