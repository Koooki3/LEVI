"use client";
import { useRef, useState } from "react";
import { ListChecks } from "lucide-react";
import {
  Badge,
  Button,
  SegmentedControl,
  Table,
  TableRow,
} from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { leviRequest } from "@/components/levi-api";
import { Note, RequestProblem } from "@/components/pages-ui/feedback";
import {
  DIRECTION_HINTS,
  DIRECTION_LABELS,
  MAX_RELEASE_HINTS,
  MAX_RELEASE_LABELS,
  ON_INELIGIBLE_HINTS,
  ON_INELIGIBLE_LABELS,
  RELEASE_CLASS_HINTS,
  RELEASE_CLASS_LABELS,
  MAX_SETTLED_ROWS,
  MIN_SETTLED_ROWS,
  analysisPayload,
  clampSettledRows,
  isResetDirection,
  parseBridges,
  previewText,
  releaseReasonLabel,
  releaseTone,
  shortKey,
  sortedClasses,
  sortedReasons,
  templateProblem,
  type ResetState,
} from "./reset";
import {
  REASON_LABELS,
  RESET_DIRECTIONS,
  type Recipe,
  type ResetAnalysis,
  type ResetEpisodeVerdict,
  type ResetMaxRelease,
  type ResetOnIneligible,
} from "./types";

const DEFAULT_LIMIT = 12;

/** What stops an export with this reset form, in words (English key for t()),
 * or null. */
export function resetFormProblem(state: ResetState): string | null {
  if (state.direction === "forward_only") return null;
  if (templateProblem(state.taskTemplate))
    return "Fix the reset instruction template above.";
  if (parseBridges(state.bridgesText).invalid.length)
    return "Fix the recorded-stretch lines above.";
  return null;
}

function Verdict({ episode }: { episode: ResetEpisodeVerdict }) {
  const { t } = useLocale();
  if (episode.eligible)
    return episode.scope === "partial" ? (
      <Badge tone="info">{t("Partly reversible")}</Badge>
    ) : (
      <Badge tone="success">{t("Reversible")}</Badge>
    );
  const reason = episode.reason ?? "";
  return (
    <>
      <Badge tone="warning">
        {t(REASON_LABELS[reason] || reason || "Not reversible")}
      </Badge>
      {episode.detail && (
        <small className="pg-pool-muted"> {episode.detail}</small>
      )}
    </>
  );
}

export function AnalysisResult({
  result,
  stale,
}: {
  result: ResetAnalysis;
  stale: boolean;
}) {
  const { t } = useLocale();
  const { summary } = result;
  const reasons = sortedReasons(summary.reasons);
  const classes = sortedClasses(summary.release_classes);
  const rest = summary.episodes - summary.reversible;
  return (
    <div className="pg-pool-preview" aria-live="polite">
      {stale && (
        <Note tone="warning" role="status">
          {t(
            "The selection or the reset settings changed since this check. Check again to see the current result.",
          )}
        </Note>
      )}
      <p className="pg-pool-hint">
        {t("Checked {analyzed} of {selected} selected episodes")
          .replace("{analyzed}", result.analyzed.toLocaleString())
          .replace("{selected}", result.selected.toLocaleString())}
        {result.analyzed < result.selected &&
          ` · ${t("the first ones in export order")}`}
      </p>
      <div className="pg-pool-stats">
        <div>
          <strong>{summary.episodes.toLocaleString()}</strong>
          <span>{t("Checked")}</span>
        </div>
        <div>
          <strong>{summary.reversible.toLocaleString()}</strong>
          <span>{t("Can be reversed")}</span>
        </div>
        <div>
          <strong>{rest.toLocaleString()}</strong>
          <span>{t("Cannot be reversed")}</span>
        </div>
      </div>
      {reasons.length > 0 && (
        <table className="pg-pool-reasons">
          <caption>{t("Why episodes cannot be reversed")}</caption>
          <tbody>
            {reasons.map(([code, n]) => (
              <tr key={code}>
                <th scope="row">{t(REASON_LABELS[code] || code)}</th>
                <td className="num">{n.toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {classes.length > 0 && (
        <div className="pg-pool-constraints">
          <span className="pg-pool-label">
            {t("Where the object went after each release")}
          </span>
          {classes.map(([cls, n]) => (
            <Badge key={cls} tone={releaseTone(cls)}>
              {t(RELEASE_CLASS_LABELS[cls])} {n.toLocaleString()}
            </Badge>
          ))}
        </div>
      )}
      {classes.length > 0 && (
        <ul className="pg-pool-hint">
          {classes.map(([cls]) => (
            <li key={cls}>
              {t(RELEASE_CLASS_LABELS[cls])}
              {": "}
              {t(RELEASE_CLASS_HINTS[cls])}
            </li>
          ))}
        </ul>
      )}
      <Table density="compact" caption={t("Reversibility of each episode")}>
        <thead>
          <tr>
            <th scope="col">{t("Episode")}</th>
            <th scope="col">{t("Result")}</th>
            <th scope="col">{t("Releases")}</th>
          </tr>
        </thead>
        <tbody>
          {result.episodes.map((episode) => (
            <TableRow key={episode.key}>
              <td>
                <code className="pg-pool-ellipsis">
                  {shortKey(episode.key)}
                </code>
              </td>
              <td>
                <Verdict episode={episode} />
              </td>
              <td>
                {episode.releases.length === 0
                  ? "—"
                  : episode.releases.map((release, index) => (
                      <div key={`${release.row}-${index}`}>
                        <Badge tone={releaseTone(release.class)}>
                          {t(
                            RELEASE_CLASS_LABELS[release.class] ?? "Not judged",
                          )}
                        </Badge>
                        {release.reason && (
                          <small className="pg-pool-muted">
                            {" "}
                            {t(releaseReasonLabel(release.reason))}
                          </small>
                        )}
                      </div>
                    ))}
              </td>
            </TableRow>
          ))}
        </tbody>
      </Table>
      <p className="pg-pool-hint">
        {t(
          "This is an image-based estimate of whether the object stays within the fingers' reach after a release, not a guarantee. Thresholds: profile",
        )}{" "}
        <code>{result.profile}</code>
      </p>
      <p className="pg-pool-hint">
        {t(
          "Settling needs the arm to stay still for at least two frames after the release. Pausing about 0.5 s after opening the gripper before lifting the arm makes many more episodes reversible.",
        )}
      </p>
    </div>
  );
}

/** The "Reset data" part of the export panel (LeRobot v2.1 only): direction,
 * instruction, tolerance, advanced settings and the reversibility check. */
export function ResetPanel({
  state,
  onChange,
  recipe,
  cameras,
  cameraMap,
  firstTask,
}: {
  state: ResetState;
  onChange: (patch: Partial<ResetState>) => void;
  recipe: Recipe;
  cameras: Record<string, string>;
  cameraMap: Record<string, string>;
  /** The first task's text, for the instruction preview. */
  firstTask: string;
}) {
  const { t } = useLocale();
  const enabled = state.direction !== "forward_only";
  const templateError = templateProblem(state.taskTemplate);
  const bridges = parseBridges(state.bridgesText);
  const [limit, setLimit] = useState(DEFAULT_LIMIT);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [checked, setChecked] = useState<{
    key: string;
    result: ResetAnalysis;
  } | null>(null);
  const latest = useRef(0);
  const request = {
    recipe: { ...recipe, name: recipe.name || "untitled" },
    reset: analysisPayload(state),
    limit,
    cameras,
    camera_map: cameraMap,
  };
  const requestKey = JSON.stringify(request);
  const canCheck =
    enabled &&
    recipe.tasks.length > 0 &&
    !templateError &&
    bridges.invalid.length === 0 &&
    !busy;
  const whyNot = canCheck
    ? ""
    : busy
      ? ""
      : recipe.tasks.length === 0
        ? "Add at least one task to the composition."
        : templateError || bridges.invalid.length
          ? "Fix the reset settings above first."
          : "";
  async function check() {
    const mine = ++latest.current;
    setError("");
    setBusy(true);
    try {
      const result = await leviRequest<ResetAnalysis>(
        "POST",
        "pool/reset/analyze",
        request,
      );
      if (mine === latest.current) setChecked({ key: requestKey, result });
    } catch (e) {
      if (mine === latest.current)
        setError(e instanceof Error ? e.message : String(e));
    } finally {
      if (mine === latest.current) setBusy(false);
    }
  }
  return (
    <div className="pg-pool-stack wide" role="group" aria-labelledby="reset-h">
      <h3 id="reset-h">{t("Reset data")}</h3>
      <p className="pg-pool-hint">
        {t(
          "A reset episode is a successful demonstration played backwards, with the instruction “Reset: <task>”. It teaches a policy to put things back.",
        )}
      </p>
      <div className="pg-pool-stack">
        <span>{t("Data direction")}</span>
        <SegmentedControl
          label={t("Data direction")}
          value={state.direction}
          options={RESET_DIRECTIONS.map((value) => ({
            value,
            label: t(DIRECTION_LABELS[value]),
          }))}
          onChange={(value) => {
            if (isResetDirection(value)) onChange({ direction: value });
          }}
        />
        <small className="pg-pool-hint">
          {t(DIRECTION_HINTS[state.direction])}
        </small>
      </div>
      {enabled && (
        <>
          <Note tone="info">
            {t(
              "A reset export does not filter out still frames: the frames where the object settles are what the check relies on.",
            )}
          </Note>
          <div className="pg-pool-fields">
            <label className="wide">
              <span>{t("Reset instruction template")}</span>
              <input
                className="ds-input ds-focus"
                value={state.taskTemplate}
                aria-invalid={!!templateError}
                aria-describedby="reset-template-hint"
                onChange={(e) => onChange({ taskTemplate: e.target.value })}
              />
              {templateError && (
                <small className="pg-pool-bad" role="alert">
                  {t(templateError)}
                </small>
              )}
              <small id="reset-template-hint" className="pg-pool-hint">
                {t("Preview")}
                {": "}
                <code>
                  {previewText(state.taskTemplate, firstTask || "<task>") ||
                    "—"}
                </code>
              </small>
            </label>
            <label className="wide">
              <span>{t("Worst release allowed")}</span>
              <select
                className="ds-input ds-focus"
                value={state.maxRelease}
                aria-describedby="reset-release-hint"
                onChange={(e) =>
                  onChange({ maxRelease: e.target.value as ResetMaxRelease })
                }
              >
                {(Object.keys(MAX_RELEASE_LABELS) as ResetMaxRelease[]).map(
                  (m) => (
                    <option key={m} value={m}>
                      {t(MAX_RELEASE_LABELS[m])}
                    </option>
                  ),
                )}
              </select>
              <small id="reset-release-hint" className="pg-pool-hint">
                {t(MAX_RELEASE_HINTS[state.maxRelease])}
              </small>
            </label>
            <label className="wide">
              <span>{t("When an episode cannot be reversed whole")}</span>
              <select
                className="ds-input ds-focus"
                value={state.onIneligible}
                aria-describedby="reset-partial-hint"
                onChange={(e) =>
                  onChange({
                    onIneligible: e.target.value as ResetOnIneligible,
                  })
                }
              >
                {(Object.keys(ON_INELIGIBLE_LABELS) as ResetOnIneligible[]).map(
                  (m) => (
                    <option key={m} value={m}>
                      {t(ON_INELIGIBLE_LABELS[m])}
                    </option>
                  ),
                )}
              </select>
              <small id="reset-partial-hint" className="pg-pool-hint">
                {t(ON_INELIGIBLE_HINTS[state.onIneligible])}
              </small>
            </label>
          </div>
          <details className="pg-pool-picked">
            <summary>{t("Advanced reset settings")}</summary>
            <div className="pg-pool-fields">
              <label className="wide">
                <span>{t("Action contract")}</span>
                <input
                  className="ds-input ds-focus"
                  value={state.actionContract}
                  onChange={(e) => onChange({ actionContract: e.target.value })}
                />
                <small className="pg-pool-hint">
                  {t(
                    "How the recorded actions are read (which number is the gripper command). Reversal is refused for sources that do not follow it.",
                  )}
                </small>
              </label>
              <label className="wide">
                <span>{t("Release camera")}</span>
                <input
                  className="ds-input ds-focus"
                  value={state.releaseCamera}
                  onChange={(e) => onChange({ releaseCamera: e.target.value })}
                />
                <small className="pg-pool-hint">
                  {t(
                    "The output camera key the object is looked for in after a release.",
                  )}
                </small>
              </label>
              <label className="wide">
                <span>{t("Frames in a row that show the object settled")}</span>
                <input
                  className="ds-input ds-focus"
                  type="number"
                  min={MIN_SETTLED_ROWS}
                  max={MAX_SETTLED_ROWS}
                  step={1}
                  value={state.minSettledRows}
                  aria-describedby="reset-settled-hint"
                  onChange={(e) =>
                    onChange({
                      minSettledRows: clampSettledRows(Number(e.target.value)),
                    })
                  }
                />
                <small id="reset-settled-hint" className="pg-pool-hint">
                  {t(
                    "2 is the default and the safer choice. 1 trusts a single still frame: more episodes pass, but some of them may show an object that is still falling.",
                  )}
                </small>
              </label>
              <label className="pg-pool-check wide">
                <input
                  type="checkbox"
                  checked={state.allowNoGrasp}
                  aria-describedby="reset-nograsp-hint"
                  onChange={() =>
                    onChange({ allowNoGrasp: !state.allowNoGrasp })
                  }
                />
                <span>
                  {t("Also reverse episodes without a grasp")}
                  <small id="reset-nograsp-hint" className="pg-pool-hint">
                    {" "}
                    {t(
                      "Risky: pushing, pouring and wiping make no physical sense backwards (the object is pulled back, the liquid flows up).",
                    )}
                  </small>
                </span>
              </label>
              <label className="wide">
                <span>{t("Local vision model connection (optional)")}</span>
                <input
                  className="ds-input ds-focus"
                  value={state.reviewModel}
                  aria-describedby="reset-review-hint"
                  onChange={(e) => onChange({ reviewModel: e.target.value })}
                />
                <small id="reset-review-hint" className="pg-pool-hint">
                  {t(
                    "The model can only veto a release the image check accepted; it can never let one through.",
                  )}
                </small>
              </label>
              <label className="wide">
                <span>
                  {t("Recorded stretches (forward key = recorded key)")}
                </span>
                <textarea
                  className="ds-input ds-focus"
                  rows={3}
                  value={state.bridgesText}
                  aria-invalid={bridges.invalid.length > 0}
                  aria-describedby="reset-bridges-hint"
                  placeholder={t("forward episode key = recorded episode key")}
                  onChange={(e) => onChange({ bridgesText: e.target.value })}
                />
                {bridges.invalid.length > 0 && (
                  <small className="pg-pool-bad" role="alert">
                    {t(
                      "Lines {lines} are not “forward key = recorded key”.",
                    ).replace("{lines}", bridges.invalid.join(", "))}
                  </small>
                )}
                <small id="reset-bridges-hint" className="pg-pool-hint">
                  {t(
                    "For an episode that cannot be reversed: another pool episode that records the real approach and grasp of the object where it came to rest. One pair per line.",
                  )}
                </small>
              </label>
            </div>
          </details>
          <div className="pg-row">
            <Button
              icon={ListChecks}
              loading={busy}
              disabled={!canCheck && !busy}
              aria-describedby={whyNot ? "reset-check-whynot" : undefined}
              onClick={() => void check()}
            >
              {t(error ? "Try the check again" : "Check reversibility")}
            </Button>
            <label className="pg-row">
              <span className="pg-pool-hint">{t("Episodes to check")}</span>
              <input
                className="ds-input ds-focus"
                type="number"
                min={1}
                max={100}
                value={limit}
                onChange={(e) =>
                  setLimit(
                    Math.min(100, Math.max(1, Number(e.target.value) || 1)),
                  )
                }
              />
            </label>
          </div>
          {whyNot && (
            <p id="reset-check-whynot" className="pg-pool-hint">
              {t(whyNot)}
            </p>
          )}
          {busy && (
            <p role="status" className="pg-pool-hint">
              {t(
                "Reading the video of the first {n} episodes. This takes a few seconds to a minute.",
              ).replace("{n}", String(limit))}
            </p>
          )}
          {error && (
            <RequestProblem
              action="The reversibility check did not run"
              message={error}
              onRetry={() => void check()}
            />
          )}
          {checked && (
            <AnalysisResult
              result={checked.result}
              stale={checked.key !== requestKey}
            />
          )}
        </>
      )}
    </div>
  );
}
