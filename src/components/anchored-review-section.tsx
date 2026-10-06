"use client";

/**
 * ANCHORED REVIEW section of the annotations timeline: the newest anchored
 * review of this episode (backend `anchored/*` routes, levi/agent/anchored.py).
 *
 * One marker per recorded robot event (e.g. each gripper opening) at its
 * frame: green = valid (every condition of the spec's rule holds), red =
 * contradicted, amber = unknown. Hover shows the model's answers and each
 * condition's reading; click seeks to the event. The header names the spec
 * by its title (its id on hover),
 * the review's outcome for the episode and the valid events.
 *
 * Rendered inside `.tl-tracks` like the value-model section; it renders
 * nothing until a result exists, so datasets without one are unchanged.
 */

import React, { useEffect, useMemo, useState } from "react";
import { Badge, Tooltip } from "@/components/ds";
import { T, useLocale } from "@/components/levi-locale";
import { useAnnotations } from "@/context/annotations-context";
import {
  anchoredMarkers,
  anchoredSpecTitle,
  anchoredSummary,
} from "@/components/anchored-lanes";
import {
  fetchAnchoredEpisode,
  isAnnotateBackendEnabled,
} from "@/utils/annotationsClient";
import type { AnchoredEpisode } from "@/types/anchored.types";

interface Props {
  duration: number;
  onSeek: (t: number) => void;
  onBandClick: (e: React.MouseEvent) => void;
  onHoverMove: (e: React.MouseEvent) => void;
  onHoverLeave: () => void;
  showTip: (e: React.MouseEvent, meta: string, text: string) => void;
  moveTip: (e: React.MouseEvent) => void;
  hideTip: () => void;
}

export const AnchoredReviewSection: React.FC<Props> = ({
  duration,
  onSeek,
  onBandClick,
  onHoverMove,
  onHoverLeave,
  showTip,
  moveTip,
  hideTip,
}) => {
  const { episodeId, ident } = useAnnotations();
  const { t, language } = useLocale();
  const repoId = ident.repoId ?? null;
  const [record, setRecord] = useState<AnchoredEpisode | null>(null);

  const enabled = isAnnotateBackendEnabled() && !!repoId && episodeId != null;

  useEffect(() => {
    setRecord(null);
    if (!enabled || !repoId || episodeId == null) return;
    const controller = new AbortController();
    fetchAnchoredEpisode(episodeId, { repoId }, controller.signal)
      .then((next) => {
        if (!controller.signal.aborted) setRecord(next);
      })
      .catch(() => {
        // Optional evidence: an unreachable result leaves the row out.
      });
    return () => controller.abort();
  }, [enabled, repoId, episodeId]);

  const markers = useMemo(
    () => anchoredMarkers(record, duration, t),
    // `t` changes with the language only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [record, duration, language],
  );

  if (!record) return null;
  return (
    <T>
      <div className="tl-section anchored-section">
        <div className="tl-section-head anchored">
          <span className="tl-section-title">
            <T>Anchored review</T>
          </span>
          <span className="tl-section-sub">
            <Tooltip
              content={`${t("Anchored review rules")} · ${t("Rule set id")}: ${record.spec.id}`}
            >
              <span tabIndex={0}>
                {anchoredSpecTitle(record.spec, language)}
              </span>
            </Tooltip>
            {" · "}
            <Tooltip
              content={t(
                "Outcome from the valid events; a person commits it in the review queue",
              )}
            >
              <span tabIndex={0}>
                <Badge
                  tone={record.outcome === "success" ? "success" : "danger"}
                >
                  {t(record.outcome)}
                </Badge>
              </span>
            </Tooltip>
            {" · "}
            {anchoredSummary(record)}
          </span>
        </div>
        <div className="tl-row">
          <div className="label">
            <span className="style-dot dot-anchored" />
            {t(
              record.event === "close"
                ? "closes"
                : record.event === "end"
                  ? "final state"
                  : "releases",
            )}
          </div>
          <div
            className="track"
            onClick={onBandClick}
            onMouseMove={onHoverMove}
            onMouseLeave={onHoverLeave}
          >
            {markers.map((m, k) =>
              m.left == null ? null : (
                <div
                  key={k}
                  className={`tl-tick anchored ${m.verdict}`}
                  style={{ left: `${m.left}%` }}
                  onClick={(e) => {
                    e.stopPropagation();
                    onSeek(m.t);
                  }}
                  onMouseEnter={(e) => showTip(e, m.meta, m.text)}
                  onMouseMove={moveTip}
                  onMouseLeave={hideTip}
                />
              ),
            )}
          </div>
        </div>
      </div>
    </T>
  );
};
