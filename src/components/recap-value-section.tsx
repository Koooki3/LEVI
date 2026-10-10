"use client";

/** Saved RECAP results are browsed independently of the compute checkpoint.
 * Explicit revision reads never change the dataset's current result. */
import React, { useCallback, useEffect, useId, useMemo, useState } from "react";
import {
  Badge,
  Button,
  Field,
  Select,
  SegmentedControl,
  Tooltip,
} from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { useAnnotations } from "@/context/annotations-context";
import { ReadOnlyReason } from "@/components/linked-dataset-notice";
import {
  AXIS_MODES,
  advantageRuns,
  datasetValueRange,
  diffDomain,
  formatSigned,
  formatTick,
  niceTicks,
  planValueAxis,
  readAxisMode,
  saveAxisMode,
  valueDiff,
  valueToPercent,
  type AxisMode,
  type ValueDomain,
  labelledFrameAtTime,
  positiveFraction,
  recapApplies,
  recapComparisonNoteKey,
  recapComparisonWarnings,
  recapSelection,
  recapView,
  thresholdSourceKey,
  valuePaths,
  type RecapSelection,
} from "@/components/recap-lanes";
import {
  cancelRecapJob,
  fetchRecapCompare,
  fetchRecapEpisode,
  fetchRecapJob,
  fetchRecapRevisions,
  fetchRecapStatus,
  fetchRecapSummary,
  isAnnotateBackendEnabled,
  runRecap,
} from "@/utils/annotationsClient";
import {
  OUTCOME_LABELS_CHANGED_EVENT,
  RECAP_UPDATED_EVENT,
  isRecapJobActive,
  type RecapComparison,
  type RecapComparisonMetric,
  type RecapEpisode,
  type RecapJob,
  type RecapRevision,
  type RecapRevisions,
  type RecapStatus,
} from "@/types/recap.types";

const POLL_MS = 1500;
const PLOT_W = 1000;
const PLOT_H = 100;

interface Props {
  duration: number;
  currentTime: number;
  onSeek: (t: number) => void;
  onBandClick: (e: React.MouseEvent) => void;
  onHoverMove: (e: React.MouseEvent) => void;
  onHoverLeave: () => void;
  showTip: (e: React.MouseEvent, meta: string, text: string) => void;
  moveTip: (e: React.MouseEvent) => void;
  hideTip: () => void;
}

/** Browser storage can be missing or throw (private windows, blocked site
 * data); the axis choice is only a per-person convenience. */
function safeStorage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

const isAbort = (error: unknown) =>
  error instanceof DOMException && error.name === "AbortError";
const message = (error: unknown) =>
  error instanceof Error ? error.message : String(error);
const percent = (fraction: number | null | undefined) =>
  fraction == null || !Number.isFinite(fraction)
    ? "—"
    : (fraction * 100).toFixed(1) + "%";
const number = (value: number | null | undefined, digits = 4) =>
  value == null || !Number.isFinite(value) ? "—" : value.toFixed(digits);
const exact = (value: number | null | undefined) =>
  value == null || !Number.isFinite(value) ? "—" : String(value);

/** The state carries its request identity so even the render before effect
 * cleanup cannot display the previous episode or version. */
function useEpisodeResult(
  repoId: string,
  episodeId: number,
  revisionId: string,
  reloadKey: number,
) {
  const key = JSON.stringify([repoId, episodeId, revisionId, reloadKey]);
  const [result, setResult] = useState<{
    key: string;
    data: RecapEpisode | null | undefined;
    error: string | null;
  }>({ key: "", data: undefined, error: null });
  useEffect(() => {
    if (!revisionId) return;
    const controller = new AbortController();
    setResult({ key, data: undefined, error: null });
    fetchRecapEpisode(episodeId, { repoId }, controller.signal, revisionId)
      .then((data) => {
        if (!controller.signal.aborted) setResult({ key, data, error: null });
      })
      .catch((error) => {
        if (!controller.signal.aborted && !isAbort(error))
          setResult({ key, data: null, error: message(error) });
      });
    return () => controller.abort();
  }, [repoId, episodeId, revisionId, key]);
  return !revisionId
    ? { data: null, error: null }
    : result.key === key
      ? result
      : { data: undefined, error: null };
}

function useComparison(
  repoId: string,
  selection: RecapSelection,
  reloadKey: number,
) {
  const { primary, comparison } = selection;
  const key = JSON.stringify([repoId, primary, comparison, reloadKey]);
  const [result, setResult] = useState<{
    key: string;
    data: RecapComparison | null | undefined;
    error: string | null;
  }>({ key: "", data: undefined, error: null });
  useEffect(() => {
    if (!primary || !comparison) return;
    const controller = new AbortController();
    setResult({ key, data: undefined, error: null });
    fetchRecapCompare({ repoId }, primary, comparison, controller.signal)
      .then((data) => {
        if (!controller.signal.aborted) setResult({ key, data, error: null });
      })
      .catch((error) => {
        if (!controller.signal.aborted && !isAbort(error))
          setResult({ key, data: null, error: message(error) });
      });
    return () => controller.abort();
  }, [repoId, primary, comparison, key]);
  return !primary || !comparison
    ? { data: null, error: null }
    : result.key === key
      ? result
      : { data: undefined, error: null };
}

/** Dataset-wide value spread of one saved result, for the "dataset range"
 * axis. Null while loading, when the result stores no per-episode minima and
 * maxima, or when the summary cannot be read: that mode is then unavailable
 * rather than guessed. */
type DatasetRange = ReturnType<typeof useDatasetRange>;

function useDatasetRange(
  repoId: string,
  revisionId: string,
  reloadKey: number,
): { range: ValueDomain | null; state: "loading" | "ready" | "failed" } {
  const key = JSON.stringify([repoId, revisionId, reloadKey]);
  const [result, setResult] = useState<{
    key: string;
    range: ValueDomain | null;
    failed: boolean;
  }>({ key: "", range: null, failed: false });
  useEffect(() => {
    if (!revisionId) return;
    const controller = new AbortController();
    fetchRecapSummary({ repoId }, controller.signal, revisionId)
      .then((summary) => {
        if (!controller.signal.aborted)
          setResult({
            key,
            range: datasetValueRange(summary),
            failed: false,
          });
      })
      .catch(() => {
        if (!controller.signal.aborted)
          setResult({ key, range: null, failed: true });
      });
    return () => controller.abort();
  }, [repoId, revisionId, key]);
  return useMemo(
    () =>
      !revisionId
        ? { range: null, state: "ready" as const }
        : result.key !== key
          ? { range: null, state: "loading" as const }
          : {
              range: result.range,
              state: result.failed ? ("failed" as const) : ("ready" as const),
            },
    [revisionId, result, key],
  );
}

function Hint({
  text,
  children,
}: {
  text?: string;
  children: React.ReactNode;
}) {
  return text ? (
    <Tooltip content={text}>
      <span tabIndex={0}>{children}</span>
    </Tooltip>
  ) : (
    <span>{children}</span>
  );
}

function RevisionFacts({
  revision,
  side,
}: {
  revision: RecapRevision;
  side: "A" | "B";
}) {
  const { t } = useLocale();
  return (
    <div className={"recap-version-facts side-" + side.toLowerCase()}>
      <strong>
        {side} · {revision.checkpoint || revision.revision_id}
      </strong>
      <code>{revision.revision_id}</code>
      <span>
        {t("threshold")} {exact(revision.threshold)} ·{" "}
        {t(thresholdSourceKey(revision.threshold_source))}
      </span>
      <span>
        {t("Return range")} [{exact(revision.return_min)},{" "}
        {exact(revision.return_max)}] · {t("lookahead")} {revision.lookahead}
      </span>
      <span>
        {t("Saved result coverage")}: {revision.episodes} {t("episodes")} ·{" "}
        {revision.frames} {t("frames")} · {percent(revision.positive_fraction)}{" "}
        {t("positive frames")}
      </span>
      <span>
        {t("Discount factor")} {exact(revision.gamma)} · {t("Failure penalty")}{" "}
        {exact(revision.failure_reward)}
      </span>
      <span>
        {t("Inference precision")} {revision.precision ?? "—"} ·{" "}
        {t("Value bins")} {revision.value_support?.num_bins ?? "—"} [
        {exact(revision.value_support?.v_min)},{" "}
        {exact(revision.value_support?.v_max)}]
      </span>
      {revision.static_filter && (
        <span>
          {t("static filter")} {revision.static_filter.kept_frames ?? "—"}/
          {revision.static_filter.frames ?? "—"}
        </span>
      )}
      {revision.stale && <Badge tone="warning">{t("stale")}</Badge>}
      {(revision.dev_only_base_models?.length ?? 0) > 0 && (
        <Hint
          text={t(
            "Computed with base-model files that are not verified official releases — for development only",
          )}
        >
          <Badge tone="warning">{t("dev base model")}</Badge>
        </Hint>
      )}
    </div>
  );
}

function ComparisonSummary({
  data,
  episodeId,
}: {
  data: RecapComparison;
  episodeId: number;
}) {
  const { t } = useLocale();
  const warnings = [
    ...new Set([
      ...recapComparisonWarnings(data.a, data.b),
      ...data.notes.map(recapComparisonNoteKey),
    ]),
  ];
  const ep = data.per_episode.find((row) => row.episode === episodeId);
  const metricRow = (
    label: string,
    metric: RecapComparisonMetric | null | undefined,
  ) => (
    <tr key={label}>
      <th scope="row">{t(label)}</th>
      <td>{number(metric?.mean_a)}</td>
      <td>{number(metric?.mean_b)}</td>
      <td>{number(metric?.mean_abs_diff)}</td>
      <td>{number(metric?.corr, 3)}</td>
    </tr>
  );
  const coverageA = data.frames.shared + data.frames.only_a;
  const coverageB = data.frames.shared + data.frames.only_b;
  return (
    <details className="recap-comparison" open>
      <summary>{t("Dataset comparison")} · A / B</summary>
      <p>
        {t(
          "Metrics use matching episode and frame IDs only; unshared frames do not enter differences or agreement.",
        )}
      </p>
      {warnings.length > 0 && (
        <ul className="recap-warnings">
          {warnings.map((warning) => (
            <li key={warning}>{t(warning)}</li>
          ))}
        </ul>
      )}
      <div
        className="recap-table-scroll"
        tabIndex={0}
        role="region"
        aria-label={t("Comparison frame coverage")}
      >
        <table className="recap-comparison-table">
          <caption>{t("Comparison frame coverage")}</caption>
          <thead>
            <tr>
              <th scope="col">{t("Coverage")}</th>
              <th scope="col">{t("Shared")}</th>
              <th scope="col">{t("Only A")}</th>
              <th scope="col">{t("Only B")}</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <th scope="row">{t("episodes")}</th>
              <td>{data.episodes.shared}</td>
              <td>{data.episodes.only_a}</td>
              <td>{data.episodes.only_b}</td>
            </tr>
            <tr>
              <th scope="row">{t("frames")}</th>
              <td>{data.frames.shared}</td>
              <td>{data.frames.only_a}</td>
              <td>{data.frames.only_b}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p>
        {t("Shared frame coverage")} · A{" "}
        {percent(coverageA ? data.frames.shared / coverageA : null)} · B{" "}
        {percent(coverageB ? data.frames.shared / coverageB : null)}
      </p>
      {data.frames.shared === 0 ? (
        <p>{t("These versions have no commonly labelled frames.")}</p>
      ) : (
        <>
          <div
            className="recap-table-scroll"
            tabIndex={0}
            role="region"
            aria-label={t("Value and advantage differences")}
          >
            <table className="recap-comparison-table">
              <caption>{t("Value and advantage differences")}</caption>
              <thead>
                <tr>
                  <th scope="col">{t("Metric")}</th>
                  <th scope="col">{t("Mean")} A</th>
                  <th scope="col">{t("Mean")} B</th>
                  <th scope="col">{t("Mean absolute difference")}</th>
                  <th scope="col">{t("Correlation")}</th>
                </tr>
              </thead>
              <tbody>
                {metricRow("Normalized Value", data.value)}
                {metricRow("Continuous advantage", data.advantage)}
                {data.value_return_units &&
                  metricRow("Value in return units", data.value_return_units)}
              </tbody>
            </table>
          </div>
          {data.value_return_units && (
            <p>
              {t(
                "Return-unit values undo each version's saved normalization; scale alignment alone does not establish model quality.",
              )}
            </p>
          )}
          {data.labels && (
            <dl className="recap-comparison-labels">
              <div>
                <dt>{t("Label agreement")}</dt>
                <dd>{percent(data.labels.agreement)}</dd>
              </div>
              <div>
                <dt>{t("Label flips")}</dt>
                <dd>
                  {data.labels.positive_a_only + data.labels.positive_b_only}
                </dd>
              </div>
              <div>
                <dt>{t("A positive / B negative")}</dt>
                <dd>{data.labels.positive_a_only}</dd>
              </div>
              <div>
                <dt>{t("A negative / B positive")}</dt>
                <dd>{data.labels.positive_b_only}</dd>
              </div>
              <div>
                <dt>{t("Both positive")}</dt>
                <dd>{data.labels.both_positive}</dd>
              </div>
              <div>
                <dt>{t("Both negative")}</dt>
                <dd>{data.labels.both_negative}</dd>
              </div>
              <div>
                <dt>{t("Positive frames on shared coverage")} A</dt>
                <dd>{percent(data.labels.positive_fraction_a)}</dd>
              </div>
              <div>
                <dt>{t("Positive frames on shared coverage")} B</dt>
                <dd>{percent(data.labels.positive_fraction_b)}</dd>
              </div>
            </dl>
          )}
          <p className="recap-episode-comparison">
            {t("Current episode comparison")}:{" "}
            {ep
              ? ep.frames +
                " " +
                t("shared frames") +
                " · " +
                t("Label agreement") +
                " " +
                percent(ep.label_agreement) +
                " · " +
                t("Value mean absolute difference") +
                " " +
                number(ep.value_mean_abs_diff)
              : t("No common labelled frames in this episode")}
          </p>
        </>
      )}
    </details>
  );
}

function AdvantageTrack({
  side,
  data,
  error,
  stateNote,
  ...props
}: Props & {
  side: "A" | "B";
  data: RecapEpisode | null | undefined;
  error: string | null;
  stateNote?: string | null;
}) {
  const { t } = useLocale();
  const runs = useMemo(
    () =>
      data
        ? advantageRuns(
            data.frame_index,
            data.positive,
            data.advantage,
            data.timestamp,
            data.fps,
            data.threshold,
          )
        : [],
    [data],
  );
  const {
    duration,
    onSeek,
    onBandClick,
    onHoverMove,
    onHoverLeave,
    showTip,
    moveTip,
    hideTip,
  } = props;
  const state =
    stateNote ??
    (error
      ? t("Result could not be loaded")
      : data === undefined
        ? t("Loading…")
        : !data || data.timestamp.length === 0
          ? t("No advantage labels for this episode")
          : null);
  return (
    <div className={"tl-row recap-advantage-row side-" + side.toLowerCase()}>
      <div className="label">
        <span className="style-dot dot-advantage" />
        {t("advantage")} {side}
      </div>
      <div
        className="track"
        onClick={onBandClick}
        onMouseMove={onHoverMove}
        onMouseLeave={onHoverLeave}
      >
        {state && (
          <span className="recap-track-state" role="status">
            {state}
          </span>
        )}
        {duration > 0 &&
          runs.map((run, k) => {
            if (run.start >= duration || run.end <= 0) return null;
            const start = Math.max(0, run.start);
            const label = t(
              run.positive ? "positive advantage" : "negative advantage",
            );
            return (
              <button
                type="button"
                key={k}
                className={
                  "tl-seg adv ds-focus " + (run.positive ? "pos" : "neg")
                }
                style={{
                  left: (start / duration) * 100 + "%",
                  width:
                    Math.max(
                      0.2,
                      ((Math.min(run.end, duration) - start) / duration) * 100,
                    ) + "%",
                  opacity: run.strength,
                }}
                aria-label={
                  side +
                  " · " +
                  label +
                  " · " +
                  run.start.toFixed(2) +
                  "s → " +
                  run.end.toFixed(2) +
                  "s · " +
                  t("mean A") +
                  " " +
                  formatSigned(run.meanAdv, 4)
                }
                onClick={(e) => {
                  e.stopPropagation();
                  onSeek(start);
                }}
                onMouseEnter={(e) =>
                  showTip(
                    e,
                    side +
                      " · " +
                      t("advantage") +
                      " · " +
                      run.count +
                      " " +
                      t("frames"),
                    label +
                      " · " +
                      t("mean A") +
                      " " +
                      formatSigned(run.meanAdv, 4),
                  )
                }
                onMouseMove={moveTip}
                onMouseLeave={hideTip}
              />
            );
          })}
      </div>
    </div>
  );
}

const AXIS_LABEL: Record<AxisMode, string> = {
  adaptive: "Adaptive",
  dataset: "Dataset range",
  fixed: "Fixed −1…0",
  return: "Return units",
};

/** Small SVG + HTML plot shared by the value and the difference rows: the
 * curves stretch with the track, the tick labels are HTML so they never
 * distort. */
function AxisPlot({
  domain,
  step,
  ticks,
  refs,
  children,
}: {
  domain: ValueDomain;
  step: number;
  ticks: number[];
  refs: number[];
  children: React.ReactNode;
}) {
  const at = (value: number) => valueToPercent(value, domain);
  return (
    <>
      <svg
        className="recap-plot"
        viewBox={"0 0 " + PLOT_W + " " + PLOT_H}
        preserveAspectRatio="none"
        aria-hidden="true"
      >
        {ticks.map((tick) => (
          <line
            key={"g" + tick}
            className="recap-grid"
            x1={0}
            x2={PLOT_W}
            y1={(at(tick) / 100) * PLOT_H}
            y2={(at(tick) / 100) * PLOT_H}
            vectorEffect="non-scaling-stroke"
          />
        ))}
        {refs
          .filter((value) => value >= domain.lo && value <= domain.hi)
          .map((value) => (
            <line
              key={"r" + value}
              className="recap-ref"
              x1={0}
              x2={PLOT_W}
              y1={(at(value) / 100) * PLOT_H}
              y2={(at(value) / 100) * PLOT_H}
              vectorEffect="non-scaling-stroke"
            />
          ))}
        {children}
      </svg>
      {ticks.map((tick) => {
        const top = at(tick);
        return (
          <span
            key={"t" + tick}
            className={
              "recap-axis" +
              (top < 12 ? " edge-top" : top > 88 ? " edge-bottom" : "")
            }
            style={{ top: top + "%" }}
          >
            {formatTick(tick, step)}
          </span>
        );
      })}
    </>
  );
}

function ValueTrack({
  dataA,
  dataB,
  comparing,
  comparisonState,
  revisionA,
  revisionB,
  datasetA,
  datasetB,
  axisMode,
  onAxisMode,
  ...props
}: Props & {
  dataA: RecapEpisode | null | undefined;
  dataB: RecapEpisode | null | undefined;
  comparing: boolean;
  comparisonState: "loading" | "error" | "ready";
  revisionA?: RecapRevision;
  revisionB?: RecapRevision;
  datasetA: DatasetRange;
  datasetB: DatasetRange;
  axisMode: AxisMode;
  onAxisMode: (mode: AxisMode) => void;
}) {
  const { t } = useLocale();
  const [expanded, setExpanded] = useState(false);
  const reasonId = useId();
  const showB = comparing && !!dataB;
  const plan = useMemo(
    () =>
      planValueAxis({
        mode: axisMode,
        a: dataA?.value,
        b: showB ? dataB?.value : null,
        revA: revisionA,
        revB: showB ? revisionB : null,
        datasetA: datasetA.range,
        datasetB: showB ? datasetB.range : null,
      }),
    [axisMode, dataA, dataB, showB, revisionA, revisionB, datasetA, datasetB],
  );
  const { domain } = plan;
  const { ticks, step } = useMemo(
    () => niceTicks(domain, expanded ? 6 : 3),
    [domain, expanded],
  );
  const pathsA = useMemo(
    () =>
      dataA
        ? valuePaths(
            plan.a,
            dataA.timestamp,
            dataA.frame_index,
            props.duration,
            PLOT_W,
            PLOT_H,
            domain,
          )
        : [],
    [dataA, plan, domain, props.duration],
  );
  const pathsB = useMemo(
    () =>
      dataB && plan.b
        ? valuePaths(
            plan.b,
            dataB.timestamp,
            dataB.frame_index,
            props.duration,
            PLOT_W,
            PLOT_H,
            domain,
          )
        : [],
    [dataB, plan, domain, props.duration],
  );
  // B − A in the axis units, on the frames both results label.
  const diff = useMemo(
    () =>
      showB && dataA && dataB && plan.b
        ? valueDiff({ ...dataA, value: plan.a }, { ...dataB, value: plan.b })
        : null,
    [showB, dataA, dataB, plan],
  );
  const diffAxis = useMemo(
    () => (diff ? diffDomain(diff.value, plan.mode, plan.scale) : null),
    [diff, plan.mode, plan.scale],
  );
  const diffTicks = useMemo(
    () => (diffAxis ? niceTicks(diffAxis, 3) : { ticks: [], step: 0 }),
    [diffAxis],
  );
  const diffPaths = useMemo(
    () =>
      diff && diffAxis
        ? valuePaths(
            diff.value,
            diff.timestamp,
            diff.frame_index,
            props.duration,
            PLOT_W,
            PLOT_H,
            diffAxis,
          )
        : [],
    [diff, diffAxis, props.duration],
  );
  const readout = (
    data: RecapEpisode | null | undefined,
    side: "A" | "B",
    series: readonly number[] | null,
  ) => {
    const frame = data
      ? labelledFrameAtTime(data.timestamp, data.fps, props.currentTime)
      : -1;
    return (
      <output
        className={"recap-frame-readout side-" + side.toLowerCase()}
        key={side}
      >
        {side} ·{" "}
        {side === "B" && comparisonState !== "ready"
          ? t(
              comparisonState === "error"
                ? "Comparison unavailable"
                : "Checking comparison…",
            )
          : data === undefined
            ? t("Loading…")
            : frame < 0 || !data
              ? t("No labelled frame at the playhead")
              : "V " +
                formatSigned(data.value[frame], 3) +
                (plan.units === "return" && series
                  ? " · " +
                    t("original return") +
                    " " +
                    formatSigned(series[frame], 1)
                  : "") +
                " · A " +
                formatSigned(data.advantage[frame], 4) +
                " · " +
                t(data.positive[frame] ? "positive" : "negative")}
      </output>
    );
  };
  const diffReadout = () => {
    if (!diff) return null;
    const i = labelledFrameAtTime(
      diff.timestamp,
      dataA?.fps ?? 0,
      props.currentTime,
    );
    return (
      <output className="recap-frame-readout side-diff" key="diff">
        B − A ·{" "}
        {i < 0
          ? t("No frame labelled by both results at the playhead")
          : "Δ " +
            formatSigned(diff.value[i], plan.units === "return" ? 2 : 3) +
            (plan.units === "return" ? " " + t("original return") : "")}
      </output>
    );
  };
  // Why a mode cannot be used. The dataset range says whether it is still
  // being read, could not be read, or is simply not stored by that result.
  const datasetStates = [datasetA.state, showB ? datasetB.state : "ready"];
  const reasons: Partial<Record<AxisMode, string>> = { ...plan.unavailable };
  if (reasons.dataset && datasetStates.includes("failed"))
    reasons.dataset = "The dataset range could not be read.";
  else if (reasons.dataset && datasetStates.includes("loading"))
    reasons.dataset = "Loading the dataset range…";
  const requestedBlocked = reasons[axisMode];
  const reasonText = AXIS_MODES.filter((mode) => reasons[mode])
    .map((mode) => t(AXIS_LABEL[mode]) + ": " + t(reasons[mode]!))
    .join(" ");
  const unit = plan.units === "return" ? t("original return") : "V";
  const rangeText =
    formatTick(domain.lo, step) + "…" + formatTick(domain.hi, step);
  return (
    <>
      <div className={"tl-row recap-value-row" + (expanded ? " expanded" : "")}>
        <div className="label">
          <span className="style-dot dot-value" />
          {t("value")}
        </div>
        <div
          className="track"
          onClick={props.onBandClick}
          onMouseMove={props.onHoverMove}
          onMouseLeave={props.onHoverLeave}
        >
          <AxisPlot domain={domain} step={step} ticks={ticks} refs={plan.refs}>
            {pathsA.map((points, i) => (
              <polyline
                key={"a-" + i}
                className="recap-line"
                points={points}
                vectorEffect="non-scaling-stroke"
              />
            ))}
            {pathsB.map((points, i) => (
              <polyline
                key={"b-" + i}
                className="recap-line comparison"
                points={points}
                vectorEffect="non-scaling-stroke"
              />
            ))}
          </AxisPlot>
          {(
            [
              ["A", dataA, plan.a],
              ["B", comparing ? dataB : null, plan.b],
            ] as const
          ).map(([side, data, series]) => {
            const frame = data
              ? labelledFrameAtTime(data.timestamp, data.fps, props.currentTime)
              : -1;
            if (
              !data ||
              !series ||
              frame < 0 ||
              !Number.isFinite(series[frame]) ||
              !(props.duration > 0)
            )
              return null;
            return (
              <span
                key={side}
                className={
                  "recap-dot side-" +
                  side.toLowerCase() +
                  " " +
                  (data.positive[frame] ? "pos" : "neg")
                }
                style={{
                  left:
                    Math.max(
                      0,
                      Math.min(1, props.currentTime / props.duration),
                    ) *
                      100 +
                    "%",
                  top: valueToPercent(series[frame], domain) + "%",
                }}
              />
            );
          })}
        </div>
      </div>
      {diff && diffAxis && (
        <div
          className={
            "tl-row recap-value-row recap-diff-row" +
            (expanded ? " expanded" : "")
          }
        >
          <div className="label">
            <span className="style-dot dot-value" />
            {t("B − A")}
          </div>
          <div
            className="track"
            onClick={props.onBandClick}
            onMouseMove={props.onHoverMove}
            onMouseLeave={props.onHoverLeave}
          >
            <AxisPlot
              domain={diffAxis}
              step={diffTicks.step}
              ticks={diffTicks.ticks}
              refs={[0]}
            >
              {diffPaths.map((points, i) => (
                <polyline
                  key={"d-" + i}
                  className="recap-diff-line"
                  points={points}
                  vectorEffect="non-scaling-stroke"
                />
              ))}
            </AxisPlot>
          </div>
        </div>
      )}
      <div className="recap-axis-controls">
        <SegmentedControl
          label={t("Value axis")}
          describedBy={reasonText ? reasonId : undefined}
          size="sm"
          value={plan.mode}
          onChange={(next) => onAxisMode(next as AxisMode)}
          options={AXIS_MODES.map((mode) => ({
            value: mode,
            label: t(AXIS_LABEL[mode]),
            disabled: !!plan.unavailable[mode],
          }))}
        />
        <Button
          size="sm"
          variant="ghost"
          aria-pressed={expanded}
          onClick={() => setExpanded((open) => !open)}
        >
          {t(expanded ? "Collapse value rows" : "Expand value rows")}
        </Button>
        {reasonText && (
          <span className="recap-axis-note" id={reasonId}>
            {reasonText}
            {requestedBlocked && " " + t("Showing the adaptive axis instead.")}
          </span>
        )}
      </div>
      <div className="recap-frame-readouts">
        {readout(dataA, "A", plan.a)}
        {comparing && readout(dataB, "B", plan.b)}
        {diffReadout()}
        <span>
          {t(
            comparing
              ? "Value curves: A solid, B dashed."
              : "Value curve: A solid.",
          )}{" "}
          {t("Axis")} {rangeText} {unit} · {t(AXIS_LABEL[plan.mode])}
        </span>
      </div>
    </>
  );
}

/** A dataset key owns status, result selection and jobs. Switching datasets
 * discards all of them, while changing episodes preserves the selected run. */
export const RecapValueSection: React.FC<Props> = (props) => {
  const { episodeId, ident, readOnly } = useAnnotations();
  const repoId = ident.repoId ?? null;
  if (
    !isAnnotateBackendEnabled() ||
    !recapApplies(repoId) ||
    !repoId ||
    episodeId == null
  )
    return null;
  return (
    <DatasetRecapSection
      key={repoId}
      {...props}
      repoId={repoId}
      episodeId={episodeId}
      readOnly={readOnly}
    />
  );
};

function DatasetRecapSection({
  repoId,
  episodeId,
  readOnly,
  ...props
}: Props & {
  repoId: string;
  episodeId: number;
  readOnly: boolean;
}) {
  const { t, language } = useLocale();
  const [status, setStatus] = useState<RecapStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [revisions, setRevisions] = useState<RecapRevisions | null>(null);
  const [revisionsError, setRevisionsError] = useState<string | null>(null);
  const [selection, setSelection] = useState<RecapSelection>({
    primary: "",
    comparison: "",
  });
  const [job, setJob] = useState<RecapJob | null>(null);
  const [runError, setRunError] = useState<string | null>(null);
  const [checkpoint, setCheckpoint] = useState("");
  const [datasetType, setDatasetType] = useState<"sft" | "rollout">("rollout");
  const [datasetTypeChosen, setDatasetTypeChosen] = useState(false);
  const [showControls, setShowControls] = useState(false);
  const [starting, setStarting] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);
  const [axisMode, setAxisMode] = useState<AxisMode>(() =>
    readAxisMode(safeStorage()),
  );
  const chooseAxisMode = useCallback((mode: AxisMode) => {
    setAxisMode(mode);
    saveAxisMode(mode, safeStorage());
  }, []);
  const blockedId = useId();
  const versionsHintId = useId();

  useEffect(() => {
    const controller = new AbortController();
    const load = async () => {
      // status collects completed worker output. Reading the revision list
      // before it resolves can permanently cache an empty or previous list.
      try {
        const next = await fetchRecapStatus({ repoId }, controller.signal);
        if (controller.signal.aborted) return;
        setStatus(next);
        setStatusError(null);
        setJob((current) =>
          next.job &&
          (isRecapJobActive(next.job) || current?.id === next.job.id)
            ? next.job
            : isRecapJobActive(current)
              ? current
              : null,
        );
      } catch (error) {
        if (controller.signal.aborted || isAbort(error)) return;
        setStatusError(message(error));
      }
      // A failed status read should still allow historical result browsing.
      try {
        const next = await fetchRecapRevisions({ repoId }, controller.signal);
        if (controller.signal.aborted) return;
        setRevisions(next);
        setRevisionsError(null);
      } catch (error) {
        if (!controller.signal.aborted && !isAbort(error))
          setRevisionsError(message(error));
      }
    };
    void load();
    return () => controller.abort();
  }, [repoId, reloadKey]);

  useEffect(() => {
    const refresh = () => setReloadKey((key) => key + 1);
    window.addEventListener(OUTCOME_LABELS_CHANGED_EVENT, refresh);
    return () =>
      window.removeEventListener(OUTCOME_LABELS_CHANGED_EVENT, refresh);
  }, []);

  // A list failure still permits browsing the known current result. The
  // visible error and refresh control remain until the full list recovers.
  const results = useMemo<RecapRevisions | null>(
    () =>
      revisions ??
      (status?.current
        ? {
            current: status.current.revision_id,
            revisions: [
              {
                ...status.current,
                step: null,
                return_min: null,
                return_max: null,
                current: true,
              },
            ],
          }
        : null),
    [revisions, status],
  );
  useEffect(() => {
    if (results) setSelection((previous) => recapSelection(results, previous));
  }, [results]);
  const primary = results?.revisions.find(
    (row) => row.revision_id === selection.primary,
  );
  const secondary = results?.revisions.find(
    (row) => row.revision_id === selection.comparison,
  );
  const episodeA = useEpisodeResult(
    repoId,
    episodeId,
    selection.primary,
    reloadKey,
  );
  const episodeB = useEpisodeResult(
    repoId,
    episodeId,
    selection.comparison,
    reloadKey,
  );
  const comparison = useComparison(repoId, selection, reloadKey);
  const datasetA = useDatasetRange(repoId, selection.primary, reloadKey);
  const datasetB = useDatasetRange(repoId, selection.comparison, reloadKey);

  const readyCheckpoints = useMemo(
    () => (status?.checkpoints ?? []).filter((c) => c.ready),
    [status],
  );
  useEffect(() => {
    const savedType = status?.current?.dataset_type;
    if (!datasetTypeChosen && (savedType === "sft" || savedType === "rollout"))
      setDatasetType(savedType);
  }, [status, datasetTypeChosen]);
  useEffect(() => {
    if (!status) return;
    setCheckpoint((selected) => {
      if (readyCheckpoints.some((c) => c.name === selected)) return selected;
      const current = status.current?.checkpoint;
      return current && readyCheckpoints.some((c) => c.name === current)
        ? current
        : (readyCheckpoints[0]?.name ?? "");
    });
  }, [status, readyCheckpoints]);

  const activeJobId = isRecapJobActive(job) ? job?.id : undefined;
  useEffect(() => {
    if (!activeJobId) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = () => {
      fetchRecapJob(activeJobId, { repoId }, controller.signal)
        .then((next) => {
          if (controller.signal.aborted) return;
          setJob(next);
          if (isRecapJobActive(next)) {
            timer = setTimeout(poll, POLL_MS);
            return;
          }
          if (next.status === "succeeded") {
            setShowControls(false);
            setReloadKey((key) => key + 1);
            window.dispatchEvent(new CustomEvent(RECAP_UPDATED_EVENT));
          } else if (next.status === "failed") {
            setRunError(next.error || t("Advantage computation failed"));
          }
        })
        .catch((error) => {
          if (!controller.signal.aborted && !isAbort(error))
            timer = setTimeout(poll, POLL_MS * 2);
        });
    };
    timer = setTimeout(poll, POLL_MS);
    return () => {
      controller.abort();
      if (timer) clearTimeout(timer);
    };
    // The failure wording need not restart job polling on a locale change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeJobId, repoId]);

  const startRun = useCallback(async () => {
    if (!checkpoint || readOnly || starting) return;
    setRunError(null);
    setStarting(true);
    try {
      setJob(
        await runRecap({ repoId }, { checkpoint, dataset_type: datasetType }),
      );
    } catch (error) {
      setRunError(message(error));
    } finally {
      setStarting(false);
    }
  }, [repoId, checkpoint, datasetType, readOnly, starting]);
  const cancelRun = useCallback(async () => {
    if (!job || readOnly) return;
    try {
      setJob(await cancelRecapJob(job.id, { repoId }));
    } catch (error) {
      setRunError(message(error));
    }
  }, [job, repoId, readOnly]);

  const jobActive = isRecapJobActive(job);
  const hasResults = (results?.revisions.length ?? 0) > 0;
  const view = recapView({
    statusLoaded: !!status,
    statusError: !!statusError,
    hasCheckpoints: (status?.checkpoints.length ?? 0) > 0,
    hasCurrent: hasResults,
    jobActive,
    episode:
      episodeA.data === undefined
        ? "loading"
        : episodeA.data && episodeA.data.timestamp.length
          ? "labels"
          : "none",
    controlsOpen: showControls,
  });
  const control = readOnly && view.control !== "progress" ? null : view.control;
  const workerReason =
    status && !status.worker.ready
      ? status.worker.reason || t("The advantage worker is not ready")
      : null;
  const computeBlocked =
    control === "compute"
      ? (workerReason ??
        (!readyCheckpoints.length
          ? t("No ready checkpoint")
          : !checkpoint
            ? t("Choose a value-model checkpoint to compute advantages.")
            : null))
      : null;
  const notReady =
    control === "compute"
      ? (status?.checkpoints ?? []).filter((c) => !c.ready)
      : [];

  const revisionLabel = (row: RecapRevision) =>
    [
      row.checkpoint || row.revision_id,
      row.step != null ? t("step") + " " + row.step : "",
      row.created_at != null && Number.isFinite(row.created_at)
        ? new Date(row.created_at * 1000).toLocaleString(
            language === "zh" ? "zh-CN" : "en",
            { hour12: false },
          )
        : "",
      row.revision_id,
      row.current ? t("Current result") : "",
    ]
      .filter(Boolean)
      .join(" · ");

  const progress = job && jobActive && (
    <span className="recap-job">
      <span className="recap-job-stage">
        {t(job.status === "queued" ? "queued" : job.progress.stage)}
        {job.progress.total > 0 &&
          " " +
            job.progress.done +
            "/" +
            job.progress.total +
            " " +
            t("frames")}
      </span>
      <span
        className="recap-progress"
        role="progressbar"
        aria-label={t("Advantage computation progress")}
        aria-valuemin={0}
        aria-valuemax={job.progress.total || 1}
        aria-valuenow={job.progress.done}
      >
        <span
          style={{
            width:
              (job.progress.total > 0
                ? Math.min(100, (100 * job.progress.done) / job.progress.total)
                : 0) + "%",
          }}
        />
      </span>
      {!readOnly && (
        <Button size="sm" onClick={cancelRun}>
          {t("Cancel")}
        </Button>
      )}
    </span>
  );
  const computeControl = status && !jobActive && (
    <span className="recap-compute">
      <Select
        aria-label={t("Value-model checkpoint for computation")}
        value={checkpoint}
        onChange={(e) => setCheckpoint(e.target.value)}
        disabled={!readyCheckpoints.length}
      >
        {!readyCheckpoints.length && (
          <option value="">{t("No ready checkpoint")}</option>
        )}
        {status.checkpoints.map((c) => (
          <option key={c.name} value={c.name} disabled={!c.ready}>
            {c.name +
              (c.provider === "fake" ? " (fake)" : "") +
              (c.ready ? "" : " — " + t("not ready"))}
          </option>
        ))}
      </Select>
      <label className="recap-compute-rule">
        <span>{t("Dataset label rule")}</span>
        <Select
          value={datasetType}
          onChange={(e) => {
            setDatasetType(e.target.value as "sft" | "rollout");
            setDatasetTypeChosen(true);
          }}
        >
          <option value="rollout">{t("Policy rollouts")}</option>
          <option value="sft">{t("Demonstrations (SFT)")}</option>
        </Select>
      </label>
      <Button
        size="sm"
        variant="primary"
        loading={starting}
        onClick={startRun}
        disabled={
          !checkpoint || starting || !!workerReason || !readyCheckpoints.length
        }
        aria-describedby={computeBlocked ? blockedId : undefined}
      >
        {t(hasResults ? "Recompute" : "Compute advantages")}
      </Button>
      {hasResults && (
        <Button
          size="sm"
          variant="ghost"
          onClick={() => {
            setShowControls(false);
            setRunError(null);
          }}
        >
          {t("Close")}
        </Button>
      )}
    </span>
  );

  return (
    <div className="tl-section recap-section">
      <div className="tl-section-head value-model">
        <span className="tl-section-title">{t("Value model")}</span>
        <span className="tl-section-sub recap-meta">
          {primary
            ? primary.checkpoint +
              " · " +
              percent(
                episodeA.data ? positiveFraction(episodeA.data.positive) : null,
              ) +
              " " +
              t("positive frames")
            : statusError
              ? t("Value model unavailable")
              : !status
                ? t("Loading…")
                : !status.checkpoints.length && !hasResults
                  ? t("No value-model checkpoint yet — add one with") +
                    " levi recap import"
                  : t("RECAP advantage labels")}
        </span>
        <span className="recap-head-right">
          {hasResults && (
            <span className="recap-legend">
              <span className="recap-swatch pos" />
              {t("positive")}
              <span className="recap-swatch neg" />
              {t("negative")}
            </span>
          )}
          {control === "progress" && progress}
          {control === "recompute" && (
            <Tooltip content={t("Compute the advantage labels again")}>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => setShowControls(true)}
              >
                {t("Recompute")}
              </Button>
            </Tooltip>
          )}
          {control === "compute" && computeControl}
          {readOnly && view.control && view.control !== "progress" && (
            <ReadOnlyReason />
          )}
        </span>
      </div>
      {(statusError || revisionsError || runError) && (
        <div className="recap-note error" role="alert">
          {statusError && (
            <div>
              {t("Value model unavailable")}: {statusError}
            </div>
          )}
          {revisionsError && (
            <div>
              {t("Could not load saved results")}: {revisionsError}
            </div>
          )}
          {runError && <div>{runError}</div>}
          <Button
            size="sm"
            variant="ghost"
            onClick={() => setReloadKey((key) => key + 1)}
          >
            {t("Refresh results")}
          </Button>
        </div>
      )}
      {job?.status === "cancelled" && (
        <div className="recap-note">{t("Advantage computation cancelled")}</div>
      )}
      {computeBlocked && (
        <div className="recap-note" id={blockedId}>
          {computeBlocked}
        </div>
      )}
      {control === "compute" && datasetType === "sft" && (
        <div className="recap-note">
          {t(
            "SFT advantages are computed, but every Boolean label is set to positive.",
          )}
        </div>
      )}
      {notReady.map((c) => (
        <div className="recap-note" key={c.name}>
          <code>{c.name}</code> {t("not ready")}: {c.reason}
        </div>
      ))}
      {hasResults && (
        <>
          <div className="recap-version-controls">
            <Field label={t("Saved result A")}>
              <Select
                value={selection.primary}
                aria-describedby={versionsHintId}
                onChange={(e) =>
                  setSelection((previous) => ({
                    primary: e.target.value,
                    comparison:
                      previous.comparison === e.target.value
                        ? ""
                        : previous.comparison,
                  }))
                }
              >
                {results?.revisions.map((row) => (
                  <option key={row.revision_id} value={row.revision_id}>
                    {revisionLabel(row)}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label={t("Compare with result B")}>
              <Select
                value={selection.comparison}
                aria-describedby={versionsHintId}
                onChange={(e) =>
                  setSelection((previous) => ({
                    ...previous,
                    comparison: e.target.value,
                  }))
                }
                disabled={(results?.revisions.length ?? 0) < 2}
              >
                <option value="">{t("No comparison")}</option>
                {results?.revisions
                  .filter((row) => row.revision_id !== selection.primary)
                  .map((row) => (
                    <option key={row.revision_id} value={row.revision_id}>
                      {revisionLabel(row)}
                    </option>
                  ))}
              </Select>
            </Field>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => setReloadKey((key) => key + 1)}
            >
              {t("Refresh results")}
            </Button>
          </div>
          <p className="recap-note" id={versionsHintId}>
            {t(
              "Browsing saved results does not change the checkpoint used for computation.",
            )}{" "}
            {t(
              "Results can cover a subset of the dataset; uncomputed episodes stay empty.",
            )}
            {(results?.revisions.length ?? 0) < 2 &&
              " " + t("Save another result to enable comparison.")}
          </p>
          <div className="recap-version-details">
            {primary && <RevisionFacts revision={primary} side="A" />}
            {secondary && <RevisionFacts revision={secondary} side="B" />}
          </div>
        </>
      )}
      {revisions && !hasResults && (
        <div className="recap-note">
          {t("No saved value-model results yet")}
        </div>
      )}
      {selection.comparison && (
        <div aria-live="polite">
          {comparison.error && (
            <div className="recap-note error" role="alert">
              {t("Could not compare these results")}: {comparison.error}
              <Button
                size="sm"
                variant="ghost"
                onClick={() => setReloadKey((key) => key + 1)}
              >
                {t("Refresh results")}
              </Button>
            </div>
          )}
          {comparison.data === undefined && (
            <div className="recap-note" role="status">
              {t("Loading dataset comparison…")}
            </div>
          )}
          {comparison.data && (
            <ComparisonSummary data={comparison.data} episodeId={episodeId} />
          )}
        </div>
      )}
      {episodeA.error && (
        <div className="recap-note error" role="alert">
          A · {t("Result could not be loaded")}: {episodeA.error}
        </div>
      )}
      {selection.comparison && episodeB.error && (
        <div className="recap-note error" role="alert">
          B · {t("Result could not be loaded")}: {episodeB.error}
        </div>
      )}
      {(hasResults || view.showRows) && (
        <>
          <AdvantageTrack
            {...props}
            side="A"
            data={episodeA.data}
            error={episodeA.error}
          />
          {selection.comparison && (
            <AdvantageTrack
              {...props}
              side="B"
              data={comparison.data ? episodeB.data : undefined}
              error={episodeB.error}
              stateNote={
                comparison.error
                  ? t("Comparison unavailable")
                  : comparison.data === undefined
                    ? t("Checking comparison…")
                    : null
              }
            />
          )}
          <ValueTrack
            {...props}
            dataA={episodeA.data}
            dataB={comparison.data ? episodeB.data : null}
            comparing={!!selection.comparison}
            revisionA={primary}
            revisionB={secondary}
            datasetA={datasetA}
            datasetB={datasetB}
            axisMode={axisMode}
            onAxisMode={chooseAxisMode}
            comparisonState={
              comparison.error
                ? "error"
                : comparison.data === undefined
                  ? "loading"
                  : "ready"
            }
          />
        </>
      )}
    </div>
  );
}
