"use client";

import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { Button } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import {
  angleToOpeningRatio,
  angleToOpeningWidthM,
  openingRatioToAngle,
  ROBOTIQ_STROKE_M,
} from "@/lib/robot-studio/robotiq-2f85";
import type { PoseFields, PreviewPose } from "./preview-state";
import {
  parseTeleopDraftFields,
  parseToolPoseFields,
  TELEOP_DRAFT_KEYS,
  teleopDraftFields,
  toolPoseFields,
  type TeleopDraft,
  type ToolDraft,
} from "./tool-draft";

export type ToolPanelProps = {
  openingRatio: number;
  onOpeningRatio: (ratio: number) => void;
  draft: ToolDraft;
  onDraft: (draft: ToolDraft) => void;
  teleopDraft: TeleopDraft;
  onTeleopDraft: (draft: TeleopDraft) => void;
  disabled?: boolean;
};

const AXES = ["X", "Y", "Z", "RX", "RY", "RZ"] as const;
// Visual interpolation only. This never populates a teleoperation draft/rate.
const PREVIEW_FULL_TRAVEL_MS = 2000;
type Hold = {
  direction: -1 | 1;
  lastTime: number;
  element: HTMLButtonElement;
  pointerId?: number;
  key?: string;
};

function PoseDraftEditor({
  kind,
  value,
  onApply,
  disabled,
}: {
  kind: "mount" | "tcp";
  value: PreviewPose | null;
  onApply: (value: PreviewPose | null) => void;
  disabled: boolean;
}) {
  const { t } = useLocale();
  const id = useId();
  const [fields, setFields] = useState<PoseFields>(() => toolPoseFields(value));
  const [invalid, setInvalid] = useState(false);
  useEffect(() => {
    setFields(toolPoseFields(value));
    setInvalid(false);
  }, [value]);
  return (
    <form
      className="rs-tool-form"
      onSubmit={(event) => {
        event.preventDefault();
        const pose = parseToolPoseFields(fields);
        setInvalid(pose === null);
        if (pose) onApply(pose);
      }}
    >
      <h3>{t("robotStudio.tool." + kind)}</h3>
      <p className="rs-meta">
        {t("robotStudio.tool." + kind + "Frame")}{" "}
        {t(
          value === null
            ? "robotStudio.tool.unconfigured"
            : "robotStudio.tool.draftConfigured",
        )}
      </p>
      <fieldset disabled={disabled}>
        <legend>{t("robotStudio.tool.poseFields")}</legend>
        <div className="rs-fields">
          {AXES.map((axis, index) => {
            const unit = index < 3 ? "mm" : "°";
            const label =
              t("robotStudio.tool." + kind) + " " + axis + " (" + unit + ")";
            return (
              <label key={axis}>
                <span>
                  {axis}
                  <small>{unit}</small>
                </span>
                <input
                  className="ds-input ds-focus"
                  type="number"
                  aria-label={label}
                  aria-invalid={invalid || undefined}
                  aria-describedby={invalid ? id : undefined}
                  step="0.1"
                  min={index < 3 ? -2000 : -360}
                  max={index < 3 ? 2000 : 360}
                  value={fields[index]}
                  onChange={(event) => {
                    setFields(
                      (old) =>
                        old.map((field, i) =>
                          i === index ? event.target.value : field,
                        ) as PoseFields,
                    );
                    setInvalid(false);
                  }}
                />
              </label>
            );
          })}
        </div>
      </fieldset>
      {invalid && (
        <p className="rs-meta" id={id} role="alert">
          {t("robotStudio.tool.invalidPose")}
        </p>
      )}
      <div className="rs-tool-row">
        <Button type="submit" size="sm" disabled={disabled}>
          {t("robotStudio.tool.applyPose")}
        </Button>
        <Button
          size="sm"
          variant="ghost"
          disabled={disabled}
          onClick={() => {
            setFields(toolPoseFields(null));
            setInvalid(false);
            onApply(null);
          }}
        >
          {t("robotStudio.tool.clearPose")}
        </Button>
      </div>
    </form>
  );
}

function TeleopDraftEditor({
  draft,
  onDraft,
  disabled,
}: {
  draft: TeleopDraft;
  onDraft: (draft: TeleopDraft) => void;
  disabled: boolean;
}) {
  const { t } = useLocale();
  const id = useId();
  const [fields, setFields] = useState(() => teleopDraftFields(draft));
  const [invalid, setInvalid] = useState(false);
  useEffect(() => {
    setFields(teleopDraftFields(draft));
    setInvalid(false);
  }, [draft]);
  return (
    <details>
      <summary>{t("robotStudio.tool.teleop")}</summary>
      <p className="rs-meta">{t("robotStudio.tool.teleopHint")}</p>
      <form
        className="rs-tool-form"
        onSubmit={(event) => {
          event.preventDefault();
          const next = parseTeleopDraftFields(fields);
          setInvalid(next === null);
          if (next) onDraft(next);
        }}
      >
        <fieldset disabled={disabled}>
          <legend>{t("robotStudio.tool.teleopFields")}</legend>
          <div className="rs-tool-config-fields">
            {TELEOP_DRAFT_KEYS.map((key) => (
              <label key={key} className="rs-tool-field">
                <span>{t("robotStudio.tool." + key)}</span>
                <input
                  className="ds-input ds-focus"
                  type="number"
                  step="any"
                  min="0"
                  aria-label={t("robotStudio.tool." + key)}
                  aria-invalid={invalid || undefined}
                  aria-describedby={invalid ? id : undefined}
                  value={fields[key]}
                  onChange={(event) => {
                    setFields((old) => ({
                      ...old,
                      [key]: event.target.value,
                    }));
                    setInvalid(false);
                  }}
                />
              </label>
            ))}
          </div>
        </fieldset>
        {invalid && (
          <p className="rs-meta" id={id} role="alert">
            {t("robotStudio.tool.invalidTeleop")}
          </p>
        )}
        <Button type="submit" size="sm" disabled={disabled}>
          {t("robotStudio.tool.applyTeleop")}
        </Button>
      </form>
    </details>
  );
}

export function ToolPanel({
  openingRatio,
  onOpeningRatio,
  draft,
  onDraft,
  teleopDraft,
  onTeleopDraft,
  disabled = false,
}: ToolPanelProps) {
  const { t } = useLocale();
  const id = useId();
  const frame = useRef<number | null>(null);
  const hold = useRef<Hold | null>(null);
  const ratioRef = useRef(openingRatio);
  const onRatioRef = useRef(onOpeningRatio);
  const disabledRef = useRef(disabled);
  const [direction, setDirection] = useState<-1 | 1 | null>(null);
  ratioRef.current = openingRatio;
  onRatioRef.current = onOpeningRatio;
  disabledRef.current = disabled;

  const stop = useCallback((render = true) => {
    const current = hold.current;
    hold.current = null;
    if (frame.current !== null) cancelAnimationFrame(frame.current);
    frame.current = null;
    if (
      current?.pointerId !== undefined &&
      current.element.hasPointerCapture(current.pointerId)
    ) {
      current.element.releasePointerCapture(current.pointerId);
    }
    if (render) setDirection(null);
  }, []);

  useEffect(() => {
    const blur = () => stop();
    const visibility = () => {
      if (document.visibilityState === "hidden") stop();
    };
    const release = (event: PointerEvent) => {
      if (hold.current?.pointerId === event.pointerId) stop();
    };
    const keyup = (event: KeyboardEvent) => {
      if (hold.current?.key === event.key) stop();
    };
    window.addEventListener("blur", blur);
    window.addEventListener("pointerup", release, true);
    window.addEventListener("pointercancel", release, true);
    window.addEventListener("keyup", keyup, true);
    document.addEventListener("visibilitychange", visibility);
    return () => {
      window.removeEventListener("blur", blur);
      window.removeEventListener("pointerup", release, true);
      window.removeEventListener("pointercancel", release, true);
      window.removeEventListener("keyup", keyup, true);
      document.removeEventListener("visibilitychange", visibility);
      stop(false);
    };
  }, [stop]);
  useEffect(() => {
    if (disabled) stop();
  }, [disabled, stop]);

  const validRatio =
    Number.isFinite(openingRatio) && openingRatio >= 0 && openingRatio <= 1;
  const blocked = disabled || !validRatio;
  const angle = validRatio ? openingRatioToAngle(openingRatio) : null;
  const widthMm = angle === null ? null : angleToOpeningWidthM(angle) * 1000;
  const percent = angle === null ? null : angleToOpeningRatio(angle) * 100;

  const start = (session: Hold) => {
    if (blocked || hold.current || document.visibilityState === "hidden")
      return;
    hold.current = session;
    setDirection(session.direction);
    const tick = (time: number) => {
      frame.current = null;
      if (
        hold.current !== session ||
        disabledRef.current ||
        document.visibilityState === "hidden"
      ) {
        stop();
        return;
      }
      const elapsed = Math.max(0, Math.min(time - session.lastTime, 50));
      session.lastTime = time;
      const previous = ratioRef.current;
      if (!Number.isFinite(previous) || previous < 0 || previous > 1) {
        stop();
        return;
      }
      const next = Math.max(
        0,
        Math.min(
          1,
          previous + (session.direction * elapsed) / PREVIEW_FULL_TRAVEL_MS,
        ),
      );
      ratioRef.current = next;
      if (next !== previous) onRatioRef.current(next);
      if (hold.current !== session) return;
      if (next === 0 || next === 1) {
        stop();
        return;
      }
      frame.current = requestAnimationFrame(tick);
    };
    frame.current = requestAnimationFrame(tick);
  };

  const pointerStart = (
    event: ReactPointerEvent<HTMLButtonElement>,
    nextDirection: -1 | 1,
  ) => {
    if (blocked || hold.current || event.button !== 0) return;
    event.preventDefault();
    const element = event.currentTarget;
    try {
      element.setPointerCapture(event.pointerId);
    } catch {
      return;
    }
    start({
      element,
      pointerId: event.pointerId,
      direction: nextDirection,
      lastTime: performance.now(),
    });
  };
  const keyStart = (
    event: ReactKeyboardEvent<HTMLButtonElement>,
    nextDirection: -1 | 1,
  ) => {
    if (event.key !== " " && event.key !== "Enter") return;
    event.preventDefault();
    if (event.repeat) return;
    start({
      element: event.currentTarget,
      key: event.key,
      direction: nextDirection,
      lastTime: performance.now(),
    });
  };
  const pointerEnd = (event: ReactPointerEvent<HTMLButtonElement>) => {
    if (hold.current?.pointerId === event.pointerId) stop();
  };

  return (
    <section className="rs-panel-section rs-tool-panel">
      <h2>{t("robotStudio.tool.title")}</h2>
      <p className="rs-meta" id={id}>
        {t("robotStudio.tool.previewHint")}
      </p>
      <label className="rs-tool-opening">
        <span>{t("robotStudio.tool.opening")}</span>
        <output>
          {widthMm === null ? "—" : widthMm.toFixed(1)} mm ·{" "}
          {percent === null ? "—" : percent.toFixed(1)}%
        </output>
        <input
          type="range"
          min="0"
          max="1"
          step="0.001"
          value={validRatio ? openingRatio : 0}
          disabled={blocked}
          aria-label={t("robotStudio.tool.opening")}
          aria-describedby={id}
          aria-valuetext={
            widthMm === null
              ? t("robotStudio.tool.invalidOpening")
              : widthMm.toFixed(1) + " mm · " + percent!.toFixed(1) + "%"
          }
          onChange={(event) => {
            stop();
            const next = Number(event.target.value);
            ratioRef.current = next;
            onRatioRef.current(next);
          }}
        />
      </label>
      <p className="rs-meta">
        {t("robotStudio.tool.modelStroke")}{" "}
        {(ROBOTIQ_STROKE_M * 1000).toFixed(1)} mm
      </p>
      <div className="rs-tool-row">
        {([1, -1] as const).map((nextDirection) => (
          <Button
            key={nextDirection}
            size="sm"
            disabled={blocked}
            aria-pressed={direction === nextDirection}
            aria-describedby={id}
            className="rs-tool-hold"
            onPointerDown={(event) => pointerStart(event, nextDirection)}
            onPointerUp={pointerEnd}
            onPointerCancel={pointerEnd}
            onLostPointerCapture={pointerEnd}
            onKeyDown={(event) => keyStart(event, nextDirection)}
            onBlur={() => stop()}
            onContextMenu={(event) => {
              event.preventDefault();
              stop();
            }}
          >
            {t(
              nextDirection === 1
                ? "robotStudio.tool.holdOpen"
                : "robotStudio.tool.holdClose",
            )}
          </Button>
        ))}
      </div>
      {!validRatio && (
        <p className="rs-meta" role="alert">
          {t("robotStudio.tool.invalidOpening")}
        </p>
      )}
      <p className="rs-meta">
        {t(
          draft.mount === null
            ? "robotStudio.tool.mountMissing"
            : "robotStudio.tool.mountDraftOnly",
        )}
      </p>
      <details>
        <summary>{t("robotStudio.tool.transforms")}</summary>
        <PoseDraftEditor
          kind="mount"
          value={draft.mount}
          disabled={disabled}
          onApply={(mount) => onDraft({ ...draft, mount })}
        />
        <PoseDraftEditor
          kind="tcp"
          value={draft.tcp}
          disabled={disabled}
          onApply={(tcp) => onDraft({ ...draft, tcp })}
        />
      </details>
      <TeleopDraftEditor
        draft={teleopDraft}
        onDraft={onTeleopDraft}
        disabled={disabled}
      />
    </section>
  );
}

export default ToolPanel;
