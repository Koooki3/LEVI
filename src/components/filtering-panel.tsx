// Modified for LEVI (2026); see NOTICE and docs/UPSTREAM.md.
"use client";
import { Check, Copy, Flag, X } from "lucide-react";
import { Button, Card, IconButton, Spinner } from "@/components/ds";
import { T, useLocale } from "@/components/levi-locale";
import {
  AnalysisCard,
  fill,
  Meter,
  toneForRatio,
} from "@/components/viewer/analysis-ui";

import React, { useState, useMemo, useCallback } from "react";
import { useFlaggedEpisodes } from "@/context/flagged-episodes-context";
import type {
  CrossEpisodeVarianceData,
  LowMovementEpisode,
  EpisodeLengthStats,
  EpisodeLengthInfo,
} from "@/app/[org]/[dataset]/[episode]/fetch-data";
import {
  ActionVelocitySection,
  FullscreenWrapper,
} from "@/components/action-insights-panel";

// ─── Shared small components ─────────────────────────────────────

function FlagBtn({ id }: { id: number }) {
  const { has, toggle } = useFlaggedEpisodes();
  const { t } = useLocale();
  const flagged = has(id);
  return (
    <IconButton
      icon={Flag}
      size="sm"
      className="vw-flag-btn"
      pressed={flagged}
      label={t(flagged ? "Unflag episode" : "Flag for review")}
      onClick={() => toggle(id)}
    />
  );
}

function FlagAllBtn({ ids, label }: { ids: number[]; label?: string }) {
  const { addMany } = useFlaggedEpisodes();
  return (
    <Button size="sm" variant="ghost" icon={Flag} onClick={() => addMany(ids)}>
      <T>{label ?? "Flag all"}</T>
    </Button>
  );
}

// ─── Lowest-Movement Episodes ────────────────────────────────────

function LowMovementSection({ episodes }: { episodes: LowMovementEpisode[] }) {
  const { t } = useLocale();
  if (episodes.length === 0) return null;
  const maxMovement = Math.max(...episodes.map((e) => e.totalMovement), 1e-10);

  return (
    <AnalysisCard
      title="Lowest-Movement Episodes"
      actions={<FlagAllBtn ids={episodes.map((e) => e.episodeIndex)} />}
    >
      <p className="vw-a-hint">
        <T>
          Episodes with the lowest average action change per frame. Very low
          values may indicate the robot was standing still or the episode was
          recorded incorrectly.
        </T>
      </p>
      <div className="vw-a-grid vw-a-grid--wide">
        {episodes.map((ep) => {
          const ratio = ep.totalMovement / maxMovement;
          return (
            <div key={ep.episodeIndex} className="vw-a-tile vw-a-tile--row">
              <FlagBtn id={ep.episodeIndex} />
              <span className="vw-a-tile__name">
                {t(`Episode ${ep.episodeIndex}`)}
              </span>
              <Meter
                className="vw-a-tile__meter"
                ratio={Math.max(0.02, ratio)}
                tone={toneForRatio(ratio, [0.15, 0.4], false)}
              />
              <span className="vw-a-tile__nums">
                {ep.totalMovement.toFixed(2)}
              </span>
            </div>
          );
        })}
      </div>
    </AnalysisCard>
  );
}

// ─── Episode Length Filter ────────────────────────────────────────

function EpisodeLengthFilter({ episodes }: { episodes: EpisodeLengthInfo[] }) {
  const { addMany } = useFlaggedEpisodes();
  const { t } = useLocale();
  const globalMin = useMemo(
    () => Math.min(...episodes.map((e) => e.lengthSeconds)),
    [episodes],
  );
  const globalMax = useMemo(
    () => Math.max(...episodes.map((e) => e.lengthSeconds)),
    [episodes],
  );

  const [rangeMin, setRangeMin] = useState(globalMin);
  const [rangeMax, setRangeMax] = useState(globalMax);

  const outsideIds = useMemo(
    () =>
      episodes
        .filter((e) => e.lengthSeconds < rangeMin || e.lengthSeconds > rangeMax)
        .map((e) => e.episodeIndex)
        .sort((a, b) => a - b),
    [episodes, rangeMin, rangeMax],
  );

  const rangeChanged = rangeMin > globalMin || rangeMax < globalMax;
  const step =
    Math.max(0.01, Math.round((globalMax - globalMin) * 0.001 * 100) / 100) ||
    0.01;
  const span = globalMax - globalMin || 1;

  return (
    <AnalysisCard title="Episode Length Filter">
      <div className="vw-a-row" aria-hidden="true">
        <span className="vw-a-hint">{rangeMin.toFixed(1)}s</span>
        <span className="vw-a-hint">{rangeMax.toFixed(1)}s</span>
      </div>
      <div className="vw-range">
        <div className="vw-range__track" />
        <div
          className="vw-range__fill"
          style={{
            left: `${((rangeMin - globalMin) / span) * 100}%`,
            right: `${100 - ((rangeMax - globalMin) / span) * 100}%`,
          }}
        />
        <input
          type="range"
          min={globalMin}
          max={globalMax}
          step={step}
          value={rangeMin}
          aria-label={t("Minimum episode length (s)")}
          onChange={(e) =>
            setRangeMin(Math.min(Number(e.target.value), rangeMax))
          }
        />
        <input
          type="range"
          min={globalMin}
          max={globalMax}
          step={step}
          value={rangeMax}
          aria-label={t("Maximum episode length (s)")}
          onChange={(e) =>
            setRangeMax(Math.max(Number(e.target.value), rangeMin))
          }
        />
      </div>

      {rangeChanged && (
        <div className="vw-a-row">
          <span className="vw-a-hint">
            {fill(
              t(
                outsideIds.length !== 1
                  ? "{n} episodes outside range"
                  : "{n} episode outside range",
              ),
              { n: outsideIds.length },
            )}
          </span>
          {outsideIds.length > 0 && (
            <Button size="sm" icon={Flag} onClick={() => addMany(outsideIds)}>
              {fill(t("Flag {n} outside range"), { n: outsideIds.length })}
            </Button>
          )}
        </div>
      )}
    </AnalysisCard>
  );
}

// ─── Main Filtering Panel ────────────────────────────────────────

interface FilteringPanelProps {
  repoId: string;
  crossEpisodeData: CrossEpisodeVarianceData | null;
  crossEpisodeLoading: boolean;
  episodeLengthStats: EpisodeLengthStats | null;
  flatChartData: Record<string, number>[];
  onViewFlaggedEpisodes?: () => void;
}

function FlaggedIdsCopyBar({
  repoId,
  onViewEpisodes,
}: {
  repoId: string;
  onViewEpisodes?: () => void;
}) {
  const { t } = useLocale();
  const { flagged, count, clear } = useFlaggedEpisodes();
  const [copied, setCopied] = useState(false);

  const ids = useMemo(() => [...flagged].sort((a, b) => a - b), [flagged]);
  const idStr = ids.join(", ");

  const handleCopy = useCallback(() => {
    navigator.clipboard.writeText(idStr);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }, [idStr]);

  if (count === 0) return null;

  return (
    <AnalysisCard
      title="Flagged Episodes"
      meta={`(${count})`}
      actions={
        <>
          <Button
            size="sm"
            variant="ghost"
            icon={copied ? Check : Copy}
            onClick={handleCopy}
          >
            {t(copied ? "Copied" : "Copy")}
          </Button>
          <Button size="sm" variant="ghost" icon={X} onClick={clear}>
            {t("Clear")}
          </Button>
        </>
      }
    >
      <p className="vw-a-ids">{idStr}</p>
      {onViewEpisodes && (
        <Button size="sm" icon={Flag} onClick={onViewEpisodes}>
          <T>View flagged episodes</T>
        </Button>
      )}
      <Card variant="sunken" padding="compact" className="vw-a-note">
        <p className="vw-a-note-text">
          <a
            href="https://github.com/huggingface/lerobot"
            target="_blank"
            rel="noopener noreferrer"
            className="vw-link"
          >
            <T>LeRobot CLI</T>
          </a>
          <T> </T>
          <T>— delete flagged episodes:</T>
        </p>
        <pre className="vw-a-codeblock">
          <T>{`# Delete episodes (modifies original dataset)\nlerobot-edit-dataset \\\n    --repo_id ${repoId} \\\n    --operation.type delete_episodes \\\n    --operation.episode_indices "[${ids.join(", ")}]"`}</T>
        </pre>
        <pre className="vw-a-codeblock">
          <T>{`# Delete episodes and save to a new dataset (preserves original)\nlerobot-edit-dataset \\\n    --repo_id ${repoId} \\\n    --new_repo_id ${repoId}_filtered \\\n    --operation.type delete_episodes \\\n    --operation.episode_indices "[${ids.join(", ")}]"`}</T>
        </pre>
      </Card>
    </AnalysisCard>
  );
}

function FilteringPanel({
  repoId,
  crossEpisodeData,
  crossEpisodeLoading,
  episodeLengthStats,
  flatChartData,
  onViewFlaggedEpisodes,
}: FilteringPanelProps) {
  const { t } = useLocale();
  return (
    <div className="vw-a-view">
      <div>
        <h2 className="vw-a-section-title">
          <T>Filtering</T>
        </h2>
        <p className="vw-a-hint">
          <T>
            Identify and flag problematic episodes for removal. Flagged episodes
            appear in the sidebar and can be exported as a CLI command.
          </T>
        </p>
      </div>

      <FlaggedIdsCopyBar
        repoId={repoId}
        onViewEpisodes={onViewFlaggedEpisodes}
      />

      {episodeLengthStats?.allEpisodeLengths && (
        <EpisodeLengthFilter episodes={episodeLengthStats.allEpisodeLengths} />
      )}

      {crossEpisodeLoading && (
        <div role="status">
          <Spinner label={t("Loading cross-episode data…")} showLabel />
        </div>
      )}

      {crossEpisodeData?.lowMovementEpisodes && (
        <LowMovementSection episodes={crossEpisodeData.lowMovementEpisodes} />
      )}

      <FullscreenWrapper>
        <ActionVelocitySection
          data={flatChartData}
          agg={crossEpisodeData?.aggVelocity}
          numEpisodes={crossEpisodeData?.numEpisodes}
          jerkyEpisodes={crossEpisodeData?.jerkyEpisodes}
        />
      </FullscreenWrapper>
    </div>
  );
}

export default FilteringPanel;
