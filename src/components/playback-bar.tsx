// Modified for LEVI (2026); see NOTICE and docs/UPSTREAM.md.
"use client";
import { useLocale } from "./levi-locale";
import React from "react";
import {
  ChevronFirst,
  ChevronLast,
  Pause,
  Play,
  Rewind,
  FastForward,
  RotateCcw,
} from "lucide-react";
import { IconButton, Kbd } from "@/components/ds";
import { useTime } from "../context/time-context";
import { nextMark, useObjectMarks } from "./object-marks";
import "@/components/viewer/viewer.css";

export { formatClock } from "@/components/viewer/time-format";
import { formatClock } from "@/components/viewer/time-format";

const PlaybackBar: React.FC = () => {
  const { duration, isPlaying, setIsPlaying, currentTime, seek } = useTime();
  const objectMarks = useObjectMarks();

  const sliderActiveRef = React.useRef(false);
  const wasPlayingRef = React.useRef(false);
  const sliderValueRef = React.useRef(currentTime);
  const [sliderValue, setSliderValue] = React.useState(currentTime);

  // Only update sliderValue from context if not dragging
  React.useEffect(() => {
    if (!sliderActiveRef.current) {
      sliderValueRef.current = currentTime;
      setSliderValue(currentTime);
    }
  }, [currentTime]);

  const handleSliderChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const t = Number(e.target.value);
    sliderValueRef.current = t;
    setSliderValue(t);
    // Seek videos immediately while dragging (no debounce)
    seek(t);
  };

  const handleSliderMouseDown = () => {
    sliderActiveRef.current = true;
    wasPlayingRef.current = isPlaying;
    setIsPlaying(false);
  };

  const handleSliderMouseUp = () => {
    sliderActiveRef.current = false;
    // Final seek to exact slider position
    seek(sliderValueRef.current);
    if (wasPlayingRef.current) {
      setIsPlaying(true);
    }
  };

  const { t } = useLocale();
  return (
    <div className="vw-playback" role="group" aria-label={t("Playback")}>
      <div className="vw-playback-buttons">
        <IconButton
          icon={RotateCcw}
          label={t("Rewind from start")}
          size="sm"
          onClick={() => seek(0)}
        />
        <IconButton
          icon={Rewind}
          label={t("Jump backward 5 seconds")}
          size="sm"
          onClick={() => seek(Math.max(0, currentTime - 5))}
        />
        <IconButton
          icon={isPlaying ? Pause : Play}
          label={t(isPlaying ? "Pause" : "Play")}
          shortcut="Space"
          variant="secondary"
          onClick={() => setIsPlaying(!isPlaying)}
        />
        <IconButton
          icon={FastForward}
          label={t("Jump forward 5 seconds")}
          size="sm"
          onClick={() => seek(Math.min(duration, currentTime + 5))}
        />
        {objectMarks.length > 0 && (
          <IconButton
            icon={ChevronFirst}
            label={t("Previous annotated frame")}
            size="sm"
            onClick={() => {
              const target = nextMark(currentTime, -1);
              if (target != null) seek(target);
            }}
          />
        )}
      </div>
      <span className="vw-scrubber">
        <input
          type="range"
          min={0}
          max={duration}
          step={0.01}
          value={sliderValue}
          onChange={handleSliderChange}
          onMouseDown={handleSliderMouseDown}
          onMouseUp={handleSliderMouseUp}
          onTouchStart={handleSliderMouseDown}
          onTouchEnd={handleSliderMouseUp}
          className="ds-focus"
          aria-label={t("Seek video")}
          aria-valuetext={`${formatClock(sliderValue)} / ${formatClock(duration)}`}
        />
        {/* Ticks sit over the track and ignore the pointer so dragging the
            slider still works exactly as before. */}
        <span aria-hidden className="pointer-events-none absolute inset-0">
          {duration > 0 &&
            objectMarks.map((mark) => (
              <span
                key={mark}
                className="vw-scrubber-mark"
                style={{ left: `${(mark / duration) * 100}%` }}
              />
            ))}
        </span>
      </span>
      {objectMarks.length > 0 && (
        <IconButton
          icon={ChevronLast}
          label={t("Next annotated frame")}
          size="sm"
          onClick={() => {
            const target = nextMark(currentTime, 1);
            if (target != null) seek(target);
          }}
        />
      )}
      <span className="vw-time">
        {formatClock(sliderValue)} / {formatClock(duration)}
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
};

export default PlaybackBar;
