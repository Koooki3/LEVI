// Modified for LEVI (2026); see NOTICE and docs/UPSTREAM.md.
"use client";
import { useLocale } from "@/components/levi-locale";
import { Pause, Play, Spline } from "lucide-react";
import { Button, IconButton, Kbd } from "@/components/ds";
import "@/components/viewer/viewer.css";

import React from "react";

interface UrdfPlaybackBarProps {
  frame: number;
  totalFrames: number;
  fps: number;
  playing: boolean;
  onPlayPause: () => void;
  trailEnabled: boolean;
  onTrailToggle: () => void;
  onFrameChange: (e: React.ChangeEvent<HTMLInputElement>) => void;
  disabled?: boolean;
}

export default function UrdfPlaybackBar({
  frame,
  totalFrames,
  fps,
  playing,
  onPlayPause,
  trailEnabled,
  onTrailToggle,
  onFrameChange,
  disabled = false,
}: UrdfPlaybackBarProps) {
  const currentTime = totalFrames > 0 ? (frame / fps).toFixed(2) : "0.00";
  const totalTime = (totalFrames / fps).toFixed(2);

  const { t } = useLocale();
  return (
    <div
      className="vw-playback"
      style={{ position: "static", maxWidth: "none" }}
      role="group"
      aria-label={t("Playback")}
      aria-busy={disabled}
    >
      <IconButton
        icon={playing ? Pause : Play}
        label={t(playing ? "Pause" : "Play")}
        shortcut="Space"
        variant="secondary"
        onClick={onPlayPause}
        disabled={disabled}
      />
      <Button
        size="sm"
        icon={Spline}
        onClick={onTrailToggle}
        disabled={disabled}
        aria-pressed={trailEnabled}
        variant={trailEnabled ? "primary" : "secondary"}
      >
        {t("Trail")}
      </Button>

      {/* Scrubber */}
      <span className="vw-scrubber">
        <input
          type="range"
          min={0}
          max={Math.max(totalFrames - 1, 0)}
          value={frame}
          onChange={onFrameChange}
          disabled={disabled}
          className="ds-focus"
          aria-label={t("Seek frame")}
        />
      </span>
      <span className="vw-time">
        {currentTime} s / {totalTime} s
      </span>
      <span className="vw-time">
        F {frame}/{Math.max(totalFrames - 1, 0)}
      </span>

      <div className="vw-shortcut-hints" aria-hidden="true">
        <span>
          <Kbd>{t("Space")}</Kbd>
          {t("pause/unpause")}
        </span>
        <span>
          <Kbd>↑</Kbd>
          <Kbd>↓</Kbd>
          {t("prev/next episode")}
        </span>
      </div>
    </div>
  );
}
