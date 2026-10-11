"use client";
import { useState } from "react";
import Link from "next/link";
import { Badge, Button, ExpandableText, SkeletonText } from "@/components/ds";
import { Note, Problem } from "@/components/pages-ui/feedback";
import { useLocale } from "@/components/levi-locale";
import {
  annotationRunHref,
  resultEpisodeHref,
  canShowResult,
  resultDownloadHref,
  resultMatches,
  resultSessionPath,
} from "./result-api";
import { PolicyComparison } from "./policy-comparison";
import { useResults } from "./use-results";
import { count, percent } from "./stats-logic";
import type { AnalysisShare, ResultItem, ResultStatus } from "./result-types";
import type { SessionIdentity } from "./session-route";
import "./results.css";
export const RESULT_STATUS: Record<ResultStatus, string> = {
  active: "Evaluation in progress",
  waiting_annotation: "Waiting for annotation",
  partial: "Partial analysis",
  reported: "Analysis available",
  stale: "Analysis is stale",
  missing_source: "Result source missing",
  archived: "Result source archived",
  rebound: "Session source changed",
  unknown: "Result status unknown",
  blocked: "Result access blocked",
};
export function ResultState({ item }: { item: ResultItem }) {
  const { t } = useLocale();
  return (
    <Badge
      tone={
        item.status === "reported"
          ? "success"
          : item.status === "blocked" || item.status === "rebound"
            ? "warning"
            : "neutral"
      }
    >
      {t(RESULT_STATUS[item.status] ?? "Result status unknown")}
    </Badge>
  );
}
function Share({ value }: { value: AnalysisShare }) {
  const { t } = useLocale();
  return (
    <>
      <strong>{percent(value.rate)}</strong> · {count(value.n)} /{" "}
      {count(value.of)}
      {value.wilson95 && (
        <span className="pg-pool-muted">
          {" "}
          · {t("95% Wilson interval")}: {percent(value.wilson95[0])}–
          {percent(value.wilson95[1])}
        </span>
      )}
      {value.small_sample && <span> · {t("Small sample")}</span>}
    </>
  );
}
export function ResultContent({
  item,
  identity,
}: {
  item: ResultItem;
  identity: SessionIdentity;
}) {
  const { t, language } = useLocale();
  const [limit, setLimit] = useState(10);
  const allowed = canShowResult(item);
  const summary = allowed ? item.analysis?.all : null;
  const records = allowed ? item.records : [];
  return (
    <div className="pg-live-result-content">
      <ResultState item={item} />
      {item.reason && <Note tone="info">{t(item.reason)}</Note>}
      <dl className="pg-live-dl">
        <dt>{t("Source root")}</dt>
        <dd>
          <ExpandableText text={item.identity.root} mono previewLength={48} />
        </dd>
        <dt>{t("Live workspace")}</dt>
        <dd>
          <ExpandableText
            text={item.identity.workspace}
            mono
            previewLength={48}
          />
        </dd>
        <dt>{t("Policy checkpoint")}</dt>
        <dd>
          <ExpandableText
            text={item.policy?.checkpoint || "—"}
            mono
            previewLength={48}
          />
        </dd>
        <dt>{t("Policy configuration")}</dt>
        <dd>
          <ExpandableText
            text={item.policy?.config || "—"}
            mono
            previewLength={48}
          />
        </dd>
        <dt>{t("Label mode")}</dt>
        <dd>{item.label_mode ? t(item.label_mode) : "—"}</dd>
      </dl>
      {item.source?.kind === "catalog_reference" && (
        <details>
          <summary>{t("Unverified source versions")}</summary>
          <p>
            {t(
              "These are historical source references. Their report contents and download eligibility have not been verified.",
            )}
          </p>
          <ul>
            {item.source.catalog_versions?.map((version, index) => (
              <li key={index}>
                {t("Label basis")}: <code>{version.basis}</code> ·{" "}
                {t("Version")}: <code>{version.version || "—"}</code> ·{" "}
                {t("Unverified")}
              </li>
            ))}
          </ul>
        </details>
      )}
      {item.source?.missing?.length > 0 && (
        <details>
          <summary>{t("Missing source references")}</summary>
          <ul>
            {item.source.missing.map((value) => (
              <li key={value}>
                <code>{t(value)}</code>
              </li>
            ))}
          </ul>
        </details>
      )}
      <Note tone="info">
        {t(
          "Operator labels and automatic verdicts are shown separately. Automatic labels and time segments have not been reviewed by a person.",
        )}
      </Note>
      {allowed && (
        <>
          <details className="pg-live-disclosure" open={!!summary}>
            <summary>{t("Current session analysis")}</summary>
            <p className="pg-pool-hint">
              {t(
                "Downloads are generated from the current snapshot, not a fixed report version.",
              )}
            </p>
            {item.source?.snapshot && (
              <p className="pg-pool-muted">
                {t("Snapshot")}: <code>{item.source.snapshot}</code>
              </p>
            )}
            {summary ? (
              <>
                <dl className="pg-live-dl">
                  <dt>{t("Episodes")}</dt>
                  <dd>{count(summary.episodes)}</dd>
                  <dt>{t("Success rate (operator labels)")}</dt>
                  <dd>
                    <Share value={summary.success} />
                  </dd>
                  <dt>{t("Success rate (automatic verdicts)")}</dt>
                  <dd>
                    <Share value={summary.background_success} />
                  </dd>
                </dl>
                <details className="pg-live-disclosure">
                  <summary>{t("Quality and subtask analysis")}</summary>
                  <dl className="pg-live-dl">
                    <dt>{t("Operator label coverage")}</dt>
                    <dd>
                      <Share value={summary.coverage.operator} />
                    </dd>
                    <dt>{t("Automatic verdict coverage")}</dt>
                    <dd>
                      <Share value={summary.coverage.background} />
                    </dd>
                    <dt>{t("Time segment coverage")}</dt>
                    <dd>
                      <Share value={summary.coverage.segments} />
                    </dd>
                  </dl>
                  {summary.funnel.stages.length > 0 ? (
                    <figure className="pg-live-result-chart">
                      <figcaption>
                        {t("Subtask reach (automatic, unreviewed)")}
                      </figcaption>
                      <ul>
                        {summary.funnel.stages.map((stage) => (
                          <li key={stage.id}>
                            <span>{stage.id}</span>
                            {typeof stage.reached.rate === "number" &&
                              Number.isFinite(stage.reached.rate) && (
                                <meter
                                  min={0}
                                  max={1}
                                  value={Math.max(
                                    0,
                                    Math.min(1, stage.reached.rate),
                                  )}
                                  aria-label={`${stage.id}: ${percent(stage.reached.rate)}`}
                                />
                              )}
                            <span>
                              {count(stage.reached.n)} /{" "}
                              {count(stage.reached.of)} ·{" "}
                              {percent(stage.reached.rate)}
                            </span>
                          </li>
                        ))}
                      </ul>
                    </figure>
                  ) : (
                    <p className="pg-pool-hint">
                      {t(
                        "No time segment annotations are available for a funnel yet.",
                      )}
                    </p>
                  )}
                  {summary.warnings.length > 0 && (
                    <details>
                      <summary>{t("Analysis limitations")}</summary>
                      <ul>
                        {summary.warnings.map((warning, index) => (
                          <li key={index}>
                            {t(warning.code)}
                            {warning.n != null && ` · ${count(warning.n)}`}
                            {warning.rate != null &&
                              ` · ${percent(warning.rate)}`}
                          </li>
                        ))}
                      </ul>
                    </details>
                  )}
                </details>
                <nav
                  className="pg-live-pagination"
                  aria-label={t("Current snapshot downloads")}
                >
                  {(["json", "markdown"] as const).map((format) => {
                    const href = resultDownloadHref(identity, format, language);
                    return href ? (
                      <a
                        className="ds-btn ds-btn--ghost ds-btn--sm ds-focus"
                        key={format}
                        href={href}
                        download
                      >
                        {t("Download current snapshot")} ·{" "}
                        {format === "json" ? "JSON" : "Markdown"}
                      </a>
                    ) : null;
                  })}
                </nav>
              </>
            ) : (
              <Note tone="info">
                {t(
                  "No analysis records are available for this exact session yet.",
                )}
              </Note>
            )}
          </details>
          <details className="pg-live-disclosure">
            <summary>{t("Report versions and label basis")}</summary>
            {item.reports.length === 0 ? (
              <p>{t("No fixed report version is indexed.")}</p>
            ) : (
              <ul>
                {item.reports.map((report, index) => (
                  <li key={index}>
                    {t("Label basis")}: <code>{report.basis}</code> ·{" "}
                    {t("Version")}: <code>{report.version || "—"}</code> ·{" "}
                    {t("Fixed report status")}:{" "}
                    {t(
                      report.status === "current"
                        ? "Current report"
                        : report.status === "stale"
                          ? "Stale report"
                          : report.status === "missing"
                            ? "Missing report"
                            : "Unknown report status",
                    )}
                    <ul>
                      {report.files.map((file) => (
                        <li key={file}>
                          <code>{file}</code>
                        </li>
                      ))}
                    </ul>
                  </li>
                ))}
              </ul>
            )}
          </details>
          <details className="pg-live-disclosure">
            <summary>
              {t("Episodes and annotation runs")} ({records.length})
            </summary>
            <div className="pg-live-result-records">
              {records.slice(0, limit).map((record, index) => {
                const runs = item.runs.filter(
                  (run) =>
                    run.dataset === record.dataset &&
                    record.episode_index != null &&
                    run.episodes.includes(record.episode_index),
                );
                const viewer = item.viewers?.find(
                  (value) => value.dataset === record.dataset,
                );
                const viewerHref =
                  record.episode_index == null
                    ? null
                    : resultEpisodeHref(viewer, record.episode_index);
                return (
                  <article
                    key={`${record.dataset}/${record.demo}/${index}`}
                    className="pg-live-result-record"
                  >
                    <h3>
                      {viewerHref ? (
                        <Link className="ds-focus" href={viewerHref}>
                          {record.demo}
                        </Link>
                      ) : (
                        record.demo
                      )}
                    </h3>
                    <dl className="pg-live-dl">
                      <dt>{t("Operator")}</dt>
                      <dd>
                        {record.operator?.outcome
                          ? t(record.operator.outcome)
                          : "—"}
                      </dd>
                      <dt>{t("Online verdict")}</dt>
                      <dd>
                        {record.online?.outcome
                          ? t(record.online.outcome)
                          : "—"}
                      </dd>
                      <dt>{t("Automatic verdict")}</dt>
                      <dd>
                        {record.agent?.outcome ? t(record.agent.outcome) : "—"}{" "}
                        · {t("Automatic, unreviewed")}
                      </dd>
                      <dt>{t("Label basis")}</dt>
                      <dd>
                        <code>{record.agent?.rule || "—"}</code>
                      </dd>
                      <dt>{t("Annotation guideline")}</dt>
                      <dd>
                        <code>{record.spec?.guideline || "—"}</code>
                      </dd>
                      <dt>{t("Review specification version")}</dt>
                      <dd>
                        <code>
                          {record.spec?.release_review || "—"} ·{" "}
                          {record.spec?.release_review_version || "—"}
                        </code>
                      </dd>
                    </dl>
                    {record.segments && (
                      <details>
                        <summary>
                          {t("Time segments")} ({record.segments.count})
                        </summary>
                        <ul>
                          {record.segments.items.map((segment, i) => (
                            <li key={i}>
                              <code>{segment.id}</code> · {segment.start ?? "—"}
                              –{segment.end ?? "—"} s ·{" "}
                              {segment.outcome ? t(segment.outcome) : "—"}
                            </li>
                          ))}
                        </ul>
                      </details>
                    )}
                    <ul>
                      {runs.map((run) => {
                        const href = annotationRunHref(
                          run,
                          record.episode_index!,
                        );
                        const label = (
                          <>
                            {t(
                              run.kind === "temporal"
                                ? "Time segment annotation run"
                                : "Verdict annotation run",
                            )}{" "}
                            · <code>{run.run_id}</code>
                          </>
                        );
                        return (
                          <li key={`${run.dataset}/${run.kind}/${run.run_id}`}>
                            {href ? (
                              <Link className="ds-focus" href={href}>
                                {label}
                              </Link>
                            ) : (
                              <>
                                {label} ·{" "}
                                {t(
                                  "Annotation workspace entry is unavailable.",
                                )}
                              </>
                            )}
                            {run.changeset && (
                              <>
                                {" "}
                                · {t("Change set")}:{" "}
                                <code>{run.changeset}</code>
                              </>
                            )}
                          </li>
                        );
                      })}
                    </ul>
                    {runs.length === 0 && (
                      <p className="pg-pool-hint">
                        {t(
                          "No verified annotation run is linked to this episode.",
                        )}
                      </p>
                    )}
                  </article>
                );
              })}
            </div>
            {records.length > limit && (
              <Button size="sm" onClick={() => setLimit((n) => n + 10)}>
                {t("Show more episodes")}
              </Button>
            )}
          </details>
        </>
      )}
      <PolicyComparison item={item} />
    </div>
  );
}
export function SessionResults({
  identity,
  tick,
}: {
  identity: SessionIdentity;
  tick?: number | null;
}) {
  const path = resultSessionPath(identity);
  const request = useResults(
    path,
    tick == null ? null : Math.floor(tick / 30000),
  );
  return <SessionResultPanel identity={identity} request={request} />;
}
export function SessionResultPanel({
  identity,
  request,
}: {
  identity: SessionIdentity;
  request: ReturnType<typeof useResults>;
}) {
  const { t } = useLocale();
  const path = resultSessionPath(identity);
  const item = request.data && "identity" in request.data ? request.data : null;
  return (
    <section
      className="pg-live-section pg-live-results"
      id="session-analysis"
      aria-label={t("Session analysis")}
    >
      <h2>{t("Session analysis")}</h2>
      {!path ? (
        <Note tone="info">
          {t(
            "This bookmark does not identify a configured source root. Open the session from live evaluation to read its exact analysis.",
          )}
        </Note>
      ) : request.error ? (
        <Problem
          title={t("Could not read session analysis")}
          why={request.error}
          fix={
            <Button size="sm" onClick={request.refresh}>
              {t("Refresh")}
            </Button>
          }
        />
      ) : !item ? (
        <SkeletonText lines={3} />
      ) : !resultMatches(item, identity) ? (
        <Note tone="warning">
          {t(
            "Session source changed. Open the current session from live evaluation; old analysis is not reused.",
          )}
        </Note>
      ) : (
        <ResultContent key={path} item={item} identity={identity} />
      )}
    </section>
  );
}
