// Modified for LEVI (2026); see NOTICE and docs/UPSTREAM.md.
"use client";
import { T, useLocale } from "@/components/levi-locale";
import {
  ChevronLeft,
  ChevronRight,
  Circle,
  CircleCheck,
  CircleX,
  Flag,
  PanelLeft,
} from "lucide-react";
import { Button, Icon, IconButton, Select, Tooltip } from "@/components/ds";
import "@/components/viewer/viewer.css";

import Link from "next/link";
import { roundTo2 } from "@/components/viewer/time-format";
import { isTextEntry } from "@/components/viewer/text-entry";
import React, { useEffect, useId, useMemo, useRef, useState } from "react";
import { useFlaggedEpisodes } from "@/context/flagged-episodes-context";
import { EpisodeManagement } from "@/components/viewer/episode-management";
import { episodeHref } from "@/components/viewer/use-local-episodes";

import type {
  DatasetDisplayInfo,
  EpisodeOutcome,
} from "@/app/[org]/[dataset]/[episode]/fetch-data";
import type { AnnotationSummary } from "@/utils/annotationsClient";

/** Small status dots distinguishing language/event annotations from SAM3
 * object/vision annotations — deliberately two separate marks, not one
 * combined dot, so multi-track annotation work stays visually distinct. */
function AnnotationDots({
  episode,
  summary,
}: {
  episode: number;
  summary: AnnotationSummary;
}) {
  const { t } = useLocale();
  const key = String(episode);
  const hasLanguage = !!summary.language[key];
  const hasVision = !!summary.vision[key];
  if (!hasLanguage && !hasVision) return null;
  return (
    <span className="vw-episode-marks">
      {hasLanguage && (
        <Tooltip content={t("Has language/event annotations")} describe={false}>
          <span
            className="vw-mark vw-mark--language"
            role="img"
            aria-label={t("Has language/event annotations")}
          />
        </Tooltip>
      )}
      {hasVision && (
        <Tooltip
          content={t("Has object/vision (SAM3) annotations")}
          describe={false}
        >
          <span
            className="vw-mark vw-mark--vision"
            role="img"
            aria-label={t("Has object/vision (SAM3) annotations")}
          />
        </Tooltip>
      )}
    </span>
  );
}

/** Success/failure mark: shape + colour + words (a filled check circle,
 * a crossed circle, or an empty circle). Metadata outcomes (`levi_outcome`,
 * from a policy-eval rollout capture) show the plain shape; human labels get
 * a ring. With `onChange`, the mark is a button cycling the human label
 * success → failure → cleared (back to the metadata outcome, if any); an
 * episode with no outcome shows the empty circle on hover to start labeling. */
function OutcomeBadge({
  outcome,
  human,
  onChange,
}: {
  outcome: EpisodeOutcome | undefined;
  human?: boolean;
  onChange?: (next: EpisodeOutcome | null) => void;
}) {
  const { t } = useLocale();
  if (!outcome && !onChange) return null;
  const label = !outcome
    ? t("No outcome")
    : t(outcome === "success" ? "Episode succeeded" : "Episode failed");
  const glyph = !outcome
    ? Circle
    : outcome === "success"
      ? CircleCheck
      : CircleX;
  const state = outcome ?? "none";
  if (!onChange) {
    return (
      <Tooltip content={label} describe={false}>
        <span
          className="vw-outcome"
          data-outcome={state}
          data-human={human ? "true" : undefined}
          role="img"
          aria-label={label}
        >
          <Icon icon={glyph} />
        </span>
      </Tooltip>
    );
  }
  const next: EpisodeOutcome | null = !human
    ? "success"
    : outcome === "success"
      ? "failure"
      : null;
  const hint = `${label}${human ? ` (${t("labelled by a person")})` : ""} — ${t(
    "click to cycle success / failure / clear",
  )}`;
  return (
    <Tooltip content={hint} describe={false}>
      <button
        type="button"
        className="vw-outcome ds-focus"
        data-outcome={state}
        data-human={human ? "true" : undefined}
        data-roving=""
        tabIndex={-1}
        aria-label={hint}
        onClick={(event) => {
          event.stopPropagation();
          onChange(next);
        }}
      >
        <Icon icon={glyph} />
      </button>
    </Tooltip>
  );
}

/** Share of an episode's frames with a positive RECAP advantage label, as a
 * small two-colour bar (value-model result; absent without one). */
function RecapBadge({ fraction }: { fraction: number | undefined }) {
  const { t } = useLocale();
  if (fraction == null || !Number.isFinite(fraction)) return null;
  const pct = Math.round(Math.max(0, Math.min(1, fraction)) * 100);
  // Attributes are not reached by <T>; translate them here.
  const label = t(`Positive advantage: ${pct}% of frames`);
  return (
    <Tooltip content={label} describe={false}>
      <span className="vw-recap-bar" role="img" aria-label={label}>
        <span style={{ width: `${pct}%` }} />
      </span>
    </Tooltip>
  );
}

interface SidebarProps {
  datasetInfo: DatasetDisplayInfo;
  paginatedEpisodes: number[];
  /** Complete task-filtered list, used to intersect the flagged view. */
  allVisibleEpisodes?: number[];
  episodeId: number;
  totalPages: number;
  currentPage: number;
  prevPage: () => void;
  nextPage: () => void;
  showFlaggedOnly: boolean;
  onShowFlaggedOnlyChange: (v: boolean) => void;
  /** Restrict the list to episodes with a recorded "failure" outcome — only
   * meaningful (and only rendered) when episodeOutcomes has entries. */
  showFailuresOnly?: boolean;
  onShowFailuresOnlyChange?: (v: boolean) => void;
  onEpisodeSelect?: (ep: number) => void;
  /** Dataset task strings; the filter appears once there are at least two. */
  tasks?: string[];
  /** Active task filter, or null for every task. */
  taskFilter?: string | null;
  onTaskFilterChange?: (task: string | null) => void;
  /** Episodes left after the task filter, across all pages. */
  filteredEpisodeCount?: number;
  /** Per-episode language/vision annotation presence, for the status dots. */
  annotationSummary?: AnnotationSummary;
  /** Per-episode success/failure label, for policy-eval rollout datasets. */
  episodeOutcomes?: Record<string, EpisodeOutcome>;
  /** Episodes whose outcome is a human label rather than metadata. */
  humanOutcomes?: Set<string>;
  /** Makes the outcome dot editable (annotation backend available). */
  onOutcomeChange?: (episode: number, outcome: EpisodeOutcome | null) => void;
  /** Per-episode positive-advantage fraction from the RECAP value model. */
  recapFractions?: Record<string, number>;
  localDatasetName?: string;
  liveSession?: string | null;
}

const Sidebar: React.FC<SidebarProps> = ({
  datasetInfo,
  paginatedEpisodes,
  allVisibleEpisodes = paginatedEpisodes,
  episodeId,
  totalPages,
  currentPage,
  prevPage,
  nextPage,
  showFlaggedOnly,
  onShowFlaggedOnlyChange,
  showFailuresOnly = false,
  onShowFailuresOnlyChange,
  onEpisodeSelect,
  tasks = [],
  taskFilter = null,
  onTaskFilterChange,
  filteredEpisodeCount,
  annotationSummary,
  episodeOutcomes,
  humanOutcomes,
  onOutcomeChange,
  recapFractions,
  localDatasetName,
  liveSession,
}) => {
  const [mobileVisible, setMobileVisible] = useState(false);
  const sidebarId = useId();
  const toggleRef = useRef<HTMLButtonElement>(null);
  // On a narrow window the list is a drawer: Escape folds it, on the same
  // level as the inspector drawer (after a layer or menu, never from a text
  // field, before clearing the selected annotation: document bubble phase).
  useEffect(() => {
    if (!mobileVisible) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.isComposing || event.defaultPrevented)
        return;
      if (!window.matchMedia?.("(max-width: 899px)").matches) return;
      if (isTextEntry(event.target)) return;
      if (document.querySelector('[aria-modal="true"]')) return;
      event.preventDefault();
      setMobileVisible(false);
      toggleRef.current?.focus();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [mobileVisible]);
  const { flagged, count, toggle } = useFlaggedEpisodes();

  const failureEpisodes = useMemo(() => {
    if (!episodeOutcomes) return null;
    return new Set(
      Object.entries(episodeOutcomes)
        .filter(([, outcome]) => outcome === "failure")
        .map(([episode]) => Number(episode)),
    );
  }, [episodeOutcomes]);
  const failureCount = failureEpisodes?.size ?? 0;

  const displayEpisodes = useMemo(() => {
    // Either filter switches the base from "current page only" to "every
    // task-filtered episode" (matching the pre-existing flagged-only
    // behavior), then both apply as an intersection.
    const anyFilterActive =
      (showFlaggedOnly && count > 0) || (showFailuresOnly && failureCount > 0);
    let base = anyFilterActive
      ? [...allVisibleEpisodes].sort((a, b) => a - b)
      : paginatedEpisodes;
    if (showFlaggedOnly && count > 0) {
      base = base.filter((episode) => flagged.has(episode));
    }
    if (showFailuresOnly && failureEpisodes && failureCount > 0) {
      base = base.filter((episode) => failureEpisodes.has(episode));
    }
    return base;
  }, [
    allVisibleEpisodes,
    paginatedEpisodes,
    showFlaggedOnly,
    flagged,
    count,
    showFailuresOnly,
    failureEpisodes,
    failureCount,
  ]);

  const { t } = useLocale();

  // The list is ONE tab stop: the current episode's link (or the first row).
  // ←/→ move between the controls of a row (link, outcome, flag); ↑/↓ already
  // step through episodes, and focus follows the current row.
  const entryEpisode = displayEpisodes.includes(episodeId)
    ? episodeId
    : displayEpisodes[0];
  const listRef = useRef<HTMLUListElement | null>(null);
  const lastEpisode = useRef(episodeId);
  useEffect(() => {
    if (lastEpisode.current === episodeId) return;
    lastEpisode.current = episodeId;
    const list = listRef.current;
    if (!list || !list.contains(document.activeElement)) return;
    list
      .querySelector<HTMLElement>(
        '.vw-episode[aria-current="true"] .vw-episode-link',
      )
      ?.focus();
  }, [episodeId]);
  const onListKeyDown = (event: React.KeyboardEvent<HTMLUListElement>) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    const current = (event.target as HTMLElement).closest("[data-roving]");
    const rowEl = current?.closest(".vw-episode");
    if (!current || !rowEl) return;
    const stops = [...rowEl.querySelectorAll<HTMLElement>("[data-roving]")];
    const at = stops.indexOf(current as HTMLElement);
    const next = stops[at + (event.key === "ArrowRight" ? 1 : -1)];
    if (!next) return;
    event.preventDefault();
    next.focus();
  };

  const row = (episode: number) => {
    const active = episode === episodeId;
    const isFlagged = flagged.has(episode);
    const name = t(`Episode ${episode}`);
    return (
      <li key={episode}>
        <div className="vw-episode" aria-current={active ? "true" : undefined}>
          {onEpisodeSelect ? (
            <button
              type="button"
              onClick={() => onEpisodeSelect(episode)}
              className="vw-episode-link ds-focus"
              data-roving=""
              tabIndex={episode === entryEpisode ? 0 : -1}
              aria-current={active ? "page" : undefined}
            >
              {name}
            </button>
          ) : (
            <Link
              href={episodeHref(episode, liveSession)}
              className="vw-episode-link ds-focus"
              data-roving=""
              tabIndex={episode === entryEpisode ? 0 : -1}
              aria-current={active ? "page" : undefined}
            >
              {name}
            </Link>
          )}
          {annotationSummary && (
            <AnnotationDots episode={episode} summary={annotationSummary} />
          )}
          {recapFractions && (
            <RecapBadge fraction={recapFractions[String(episode)]} />
          )}
          <OutcomeBadge
            outcome={episodeOutcomes?.[String(episode)]}
            human={humanOutcomes?.has(String(episode))}
            onChange={
              onOutcomeChange
                ? (next) => onOutcomeChange(episode, next)
                : undefined
            }
          />
          <Tooltip content={t(isFlagged ? "Unflag" : "Flag")} describe={false}>
            <button
              type="button"
              onClick={() => toggle(episode)}
              className="vw-flag ds-focus"
              data-roving=""
              tabIndex={-1}
              aria-pressed={isFlagged}
              aria-label={`${t(isFlagged ? "Unflag" : "Flag")} · ${name}`}
            >
              <Icon icon={Flag} />
            </button>
          </Tooltip>
        </div>
      </li>
    );
  };

  return (
    <div className="vw-sidebar-wrap">
      <nav
        id={sidebarId}
        className="vw-sidebar"
        data-mobile-hidden={mobileVisible ? undefined : "true"}
        aria-label={t("Episode list")}
      >
        <dl className="vw-facts">
          <dt>{t("Frames")}</dt>
          <dd>{datasetInfo.total_frames.toLocaleString()}</dd>
          <dt>{t("Episodes")}</dt>
          <dd>{datasetInfo.total_episodes.toLocaleString()}</dd>
          <dt>{t("FPS")}</dt>
          <dd>{roundTo2(datasetInfo.fps)}</dd>
        </dl>

        {localDatasetName && (
          <EpisodeManagement
            name={localDatasetName}
            episodeIds={displayEpisodes}
            currentEpisode={episodeId}
            session={liveSession}
          />
        )}

        {tasks.length > 1 && onTaskFilterChange && (
          <div className="vw-sidebar-section">
            <label className="vw-label" htmlFor="vw-task-filter">
              {t("Task filter")}
            </label>
            <Select
              id="vw-task-filter"
              value={taskFilter ?? ""}
              onChange={(e) => onTaskFilterChange(e.target.value || null)}
              className="mt-1 w-full"
              aria-label={t("Filter episodes by task")}
            >
              <option value="">{t(`All tasks (${tasks.length})`)}</option>
              {tasks.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </Select>
          </div>
        )}

        <div className="vw-sidebar-section">
          <div className="vw-sidebar-head">
            <h2 className="vw-label">
              {t("Episodes")}
              {taskFilter && filteredEpisodeCount !== undefined && (
                <span className="tabular"> · {filteredEpisodeCount}</span>
              )}
            </h2>
            <div className="vw-filter-chips">
              {failureCount > 0 && onShowFailuresOnlyChange && (
                <button
                  type="button"
                  className="vw-chip ds-focus"
                  aria-pressed={showFailuresOnly}
                  onClick={() => onShowFailuresOnlyChange(!showFailuresOnly)}
                >
                  <Icon icon={CircleX} />
                  {t("Failures")} · {failureCount}
                </button>
              )}
              {count > 0 && (
                <button
                  type="button"
                  className="vw-chip ds-focus"
                  aria-pressed={showFlaggedOnly}
                  onClick={() => onShowFlaggedOnlyChange(!showFlaggedOnly)}
                >
                  <Icon icon={Flag} />
                  {t("Flagged")} · {count}
                </button>
              )}
            </div>
          </div>

          {displayEpisodes.length === 0 && (
            <p className="mt-2 px-1 vw-faint">
              <T>No episodes match this task.</T>
            </p>
          )}

          <p id="vw-episodes-hint" className="ds-sr-only">
            {t(
              "The list is one tab stop. Left and right arrows move between an episode's controls; up and down arrows change episode.",
            )}
          </p>
          <ul
            className="vw-episodes"
            ref={listRef}
            onKeyDown={onListKeyDown}
            aria-describedby="vw-episodes-hint"
            aria-keyshortcuts="ArrowLeft ArrowRight"
          >
            {displayEpisodes.map(row)}
          </ul>

          {!showFlaggedOnly && totalPages > 1 && (
            <div className="vw-pager">
              <Button
                size="sm"
                variant="ghost"
                icon={ChevronLeft}
                onClick={prevPage}
                disabled={currentPage === 1}
              >
                {t("Previous")}
              </Button>
              <span>
                {currentPage} / {totalPages}
              </span>
              <Button
                size="sm"
                variant="ghost"
                iconEnd={ChevronRight}
                onClick={nextPage}
                disabled={currentPage === totalPages}
              >
                {t("Next")}
              </Button>
            </div>
          )}
        </div>
      </nav>

      <div className="vw-sidebar-toggle">
        <IconButton
          ref={toggleRef}
          icon={PanelLeft}
          label={t("Toggle sidebar")}
          size="sm"
          aria-expanded={mobileVisible}
          aria-controls={sidebarId}
          onClick={() => setMobileVisible((prev) => !prev)}
        />
      </div>
    </div>
  );
};

export default Sidebar;
