"use client";
import { Problem } from "@/components/pages-ui/feedback";
import { useEffect, useId, useState } from "react";
import { useLocale } from "@/components/levi-locale";
import { ConfirmDialog } from "@/components/pool/confirm-dialog";
import { friendlyError } from "./friendly-error";
import { applyChange, type Requester } from "./live-actions";
import {
  clock,
  nameList,
  pruneSelection,
  removableDemos,
  removalBlock,
  shownDemos,
  toggleAll,
  toggleSelection,
  agreeOf,
  verdictReason,
} from "./live-logic";
import type { DatasetDetail, DemoRow } from "./types";

export const DEMO_STATES: Record<string, string> = {
  mirrored: "waiting",
  annotating: "labelling",
  done: "done",
  failed: "failed",
  rejected: "rejected",
  skipped_human: "skipped (a person annotated it)",
  stuck: "stuck (never finished)",
};

const AGREEMENT_TEXT: Record<string, string> = {
  yes: "agrees",
  no: "disagrees",
};

/** The operator label (ground truth) of one episode, before the agent's
 * automatic verdict and drawn differently (a solid outline, not the dashed
 * "auto" tag), with whether the agent agrees; nothing without a label (an
 * unattended episode is "unlabeled": no label). */
export function OperatorChip({ demo }: { demo: DemoRow }) {
  const { t } = useLocale();
  const outcome = demo.operator_label?.outcome;
  // Unlabeled (an unattended run) is no operator label to show.
  if (outcome !== "success" && outcome !== "failure" && outcome !== "discarded")
    return null;
  const agreement =
    demo.agreement ?? agreeOf(demo.operator_label, demo.verdict);
  const mark = agreement ? AGREEMENT_TEXT[agreement] : undefined;
  return (
    <>
      <span
        className="pg-live-optag"
        title={t("Operator label (ground truth)")}
      >
        {t("operator")}
      </span>{" "}
      {t(outcome)}
      {mark && (
        <span
          className={`pg-live-agree${agreement === "no" ? " no" : ""}`}
          data-agreement={agreement}
        >
          {t(mark)}
        </span>
      )}
      {" · "}
    </>
  );
}

/** What a removal does and does not do, in the confirmation. */
export function RemoveDialog({
  demos,
  reason,
  onReason,
  busy,
  error,
  onConfirm,
  onCancel,
}: {
  demos: string[];
  reason: string;
  onReason: (value: string) => void;
  busy: boolean;
  error: string;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const { t } = useLocale();
  const many = demos.length > 1;
  return (
    <ConfirmDialog
      open={demos.length > 0}
      title={
        many
          ? t("Remove episodes (restorable)")
          : t("Remove episode (restorable)")
      }
      confirmLabel={
        many
          ? t("Remove episodes (restorable)")
          : t("Remove episode (restorable)")
      }
      busy={busy}
      onConfirm={onConfirm}
      onCancel={onCancel}
    >
      <p>
        <code>{nameList(demos)}</code>
      </p>
      <p>
        <strong>
          {t("{n} episodes in total").replace("{n}", String(demos.length))}
        </strong>
      </p>
      <p>
        {t(
          "They stop being counted and labelled, and the training pool leaves them out. Nothing is deleted: the rollout folders and the copies LEVI mirrored stay, and annotations already written stay too.",
        )}
      </p>
      <p>
        {t(
          "You can bring them back at any time from the Removed list of this card.",
        )}
      </p>
      <label className="pg-live-reason">
        <span>{t("Reason (optional)")}</span>
        <input
          className="ds-input ds-focus grow"
          type="text"
          maxLength={300}
          value={reason}
          onChange={(e) => onReason(e.target.value)}
        />
      </label>
      {error && (
        <Problem
          title={t("Nothing was removed")}
          why={friendlyError(error, t)}
        />
      )}
    </ConfirmDialog>
  );
}

function Row({
  demo,
  dataset,
  removed,
  checked,
  block,
  open,
  onCheck,
  onOpen,
  onRemove,
  onRestore,
}: {
  demo: DemoRow;
  dataset: string;
  removed: boolean;
  checked: boolean;
  block: ReturnType<typeof removalBlock>;
  open: boolean;
  onCheck: () => void;
  onOpen: () => void;
  onRemove: () => void;
  onRestore: () => void;
}) {
  const { t } = useLocale();
  const why =
    block === "busy"
      ? t("Being labelled now: part of the batch in progress")
      : block === "not_part"
        ? t("Never taken into the dataset")
        : "";
  // The reason a removal is unavailable is words in the row and the details,
  // tied to the disabled controls, not a hover-only `title`.
  const whyId = useId();
  const showWhy = !removed && why !== "";
  return (
    <li>
      <input
        type="checkbox"
        aria-label={`${t("Select")} ${demo.demo}`}
        aria-describedby={showWhy ? `${whyId}-row` : undefined}
        checked={checked}
        disabled={!removed && block !== null}
        onChange={onCheck}
      />
      <code>{demo.demo}</code>
      <span>{t(DEMO_STATES[demo.state ?? ""] ?? demo.state ?? "—")}</span>
      {showWhy && (
        <span id={`${whyId}-row`} className="pg-pool-muted">
          {why}
        </span>
      )}
      <span>
        {demo.segments != null ? `${demo.segments} ${t("time segments")}` : "—"}
      </span>
      <span className="pg-live-autocell">
        {!removed && <OperatorChip demo={demo} />}
        {removed ? (
          <span className="pg-pool-muted">
            {demo.excluded?.reason || t("no reason given")}
            {demo.excluded?.at ? ` · ${clock(demo.excluded.at)}` : ""}
          </span>
        ) : demo.verdict ? (
          <>
            <span className="pg-live-auto-tag">{t("auto")}</span>{" "}
            {demo.verdict.undecided || !demo.verdict.outcome
              ? t("undecided")
              : t(demo.verdict.outcome)}
            {demo.verdict.events != null &&
              ` (${demo.verdict.valid_events ?? 0}/${demo.verdict.events})`}
            {verdictReason(demo.verdict) && (
              <span className="pg-pool-muted">
                {" · "}
                {t(verdictReason(demo.verdict) as string)}
              </span>
            )}
          </>
        ) : demo.reason ? (
          <span className="pg-pool-muted">{demo.reason}</span>
        ) : (
          "—"
        )}
      </span>
      {removed ? (
        <button
          type="button"
          className="ds-btn ds-btn--ghost ds-btn--sm ds-focus"
          onClick={onRestore}
        >
          {t("Restore")}
        </button>
      ) : (
        <button
          type="button"
          className="ds-btn ds-btn--ghost ds-btn--sm ds-focus"
          aria-expanded={open}
          onClick={onOpen}
        >
          {open ? t("Hide details") : t("Details")}
        </button>
      )}
      {open && !removed && (
        <div className="pg-live-demo-detail">
          <dl className="pg-live-dl">
            <dt>{t("Episode")}</dt>
            <dd>
              <code>
                {dataset} / {demo.demo}
              </code>
              {demo.episode_index != null && ` · #${demo.episode_index}`}
            </dd>
            <dt>{t("Finished")}</dt>
            <dd>{demo.completed_at ? clock(demo.completed_at) : "—"}</dd>
            <dt>{t("Attempts")}</dt>
            <dd>{demo.attempts ?? 0}</dd>
          </dl>
          <div className="pg-row pg-live-actions">
            <button
              type="button"
              className="ds-btn ds-btn--secondary ds-focus"
              disabled={block !== null}
              aria-describedby={showWhy ? `${whyId}-detail` : undefined}
              onClick={onRemove}
            >
              {t("Remove episode (restorable)")}
            </button>
            {why && (
              <span id={`${whyId}-detail`} className="pg-pool-muted">
                {why}
              </span>
            )}
          </div>
        </div>
      )}
    </li>
  );
}

/** A dataset card's episodes: the ones in the dataset (with remove, one by
 * one or selected) and, behind "Removed (n)", the ones a person took out
 * (with restore). */
export function EpisodeList({
  dataset,
  detail,
  onChanged,
  request,
  initialShowRemoved = false,
  initialSelected = [],
  initialPending = [],
}: {
  dataset: string;
  detail: DatasetDetail | undefined;
  onChanged: () => void;
  request?: Requester;
  initialShowRemoved?: boolean;
  initialSelected?: string[];
  initialPending?: string[];
}) {
  const { t } = useLocale();
  const [all, setAll] = useState(false);
  const [showRemoved, setShowRemoved] = useState(initialShowRemoved);
  const [selected, setSelected] = useState<Set<string>>(
    () => new Set(initialSelected),
  );
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [pending, setPending] = useState<string[]>(initialPending);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const batch = detail?.current?.demos;
  const removedCount = detail?.excluded_count ?? 0;
  const demos = shownDemos(detail, showRemoved);
  const shown = all ? demos : demos.slice(0, 10);
  // What can be chosen is what is on screen: "select all" and the choice
  // itself never reach rows nobody can see (collapsing the list drops them).
  const candidates = showRemoved
    ? shown.map((d) => d.demo)
    : removableDemos(shown, batch);
  const key = candidates.join(",");
  useEffect(() => {
    setSelected((prev) => pruneSelection(prev, key ? key.split(",") : []));
  }, [key]);

  if (!detail || ((detail.demos ?? []).length === 0 && removedCount === 0))
    return null;
  const chosen = [...selected];

  async function act(kind: "remove" | "restore", names: string[]) {
    setBusy(true);
    setError("");
    const outcome = await applyChange(kind, dataset, names, reason, t, request);
    setBusy(false);
    if (!outcome.ok) {
      // A restore has no dialog to show it in.
      if (kind === "restore") setNotice(friendlyError(outcome.message, t));
      else setError(outcome.message);
      return;
    }
    setNotice(outcome.notice);
    setPending([]);
    setReason("");
    setSelected(new Set());
    onChanged();
  }

  return (
    <div className="pg-live-demos">
      <div className="pg-live-demos-head">
        <strong>
          {showRemoved
            ? t("Removed episodes (not counted, not labelled)")
            : t("Episodes (newest first)")}
        </strong>
        <button
          type="button"
          className="ds-btn ds-btn--ghost ds-btn--sm ds-focus"
          aria-pressed={showRemoved}
          onClick={() => {
            setShowRemoved((v) => !v);
            setSelected(new Set());
            setAll(false);
          }}
        >
          {showRemoved
            ? t("Back to the episodes")
            : t("Removed ({n})").replace("{n}", String(removedCount))}
        </button>
      </div>
      {candidates.length > 0 && (
        <div className="pg-live-bulk">
          <label>
            <input
              type="checkbox"
              checked={candidates.every((d) => selected.has(d))}
              onChange={() => setSelected(toggleAll(selected, candidates))}
            />{" "}
            {shown.length < demos.length
              ? t("Select the shown")
              : t("Select all")}
          </label>
          {chosen.length > 0 && (
            <>
              {showRemoved ? (
                <button
                  type="button"
                  className="ds-btn ds-btn--secondary ds-focus"
                  disabled={busy}
                  onClick={() => void act("restore", chosen)}
                >
                  {t("Restore selected")} ({chosen.length})
                </button>
              ) : (
                <button
                  type="button"
                  className="ds-btn ds-btn--secondary ds-focus"
                  onClick={() => setPending(chosen)}
                >
                  {t("Remove selected")} ({chosen.length})
                </button>
              )}
              <button
                type="button"
                className="ds-btn ds-btn--ghost ds-btn--sm ds-focus"
                onClick={() => setSelected(new Set())}
              >
                {t("Clear selection")}
              </button>
            </>
          )}
        </div>
      )}
      {notice && (
        <p className="pg-pool-hint" role="status">
          {notice}
        </p>
      )}
      {demos.length === 0 ? (
        <p className="pg-pool-muted">
          {showRemoved
            ? t("No episode has been removed.")
            : t("Every episode has been removed.")}
        </p>
      ) : (
        <ul>
          {shown.map((d) => (
            <Row
              key={d.demo}
              demo={d}
              dataset={dataset}
              removed={showRemoved}
              checked={selected.has(d.demo)}
              block={removalBlock(d, batch)}
              open={open.has(d.demo)}
              onCheck={() => setSelected(toggleSelection(selected, d.demo))}
              onOpen={() => setOpen(toggleSelection(open, d.demo))}
              onRemove={() => setPending([d.demo])}
              onRestore={() => void act("restore", [d.demo])}
            />
          ))}
        </ul>
      )}
      {demos.length > 10 && (
        <button
          type="button"
          className="ds-btn ds-btn--ghost ds-btn--sm ds-focus"
          onClick={() => setAll((v) => !v)}
        >
          {all ? t("Show fewer") : `${t("Show all")} ${demos.length}`}
        </button>
      )}
      <RemoveDialog
        demos={pending}
        reason={reason}
        onReason={setReason}
        busy={busy}
        error={error}
        onConfirm={() => void act("remove", pending)}
        onCancel={() => {
          setPending([]);
          setError("");
        }}
      />
    </div>
  );
}
