"use client";
// The camera preview's controls in the Run card's live view (shown only while
// no client of the page is on): one button that opens the preview and, while it
// holds the cameras, stops it (the same element, also while it stops, so the
// keyboard focus stays where it was), what the preview is doing in words, and
// why it cannot start, has failed or has stopped by itself. The pictures are
// the live view's own (live-frames.tsx). Nothing here opens a camera: it asks
// LEVI's route, which refuses while a run is on, the arm lock is held or a
// client is connected.
import { Camera, CameraOff } from "lucide-react";
import { Button } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import en from "@/i18n/en.json";
import { WordedText, RefusedBlock } from "./action-parts";
import { wordOf } from "./ctl-text";
import { sayText } from "./marvel0-say";
import {
  holdsCameras,
  maxMinutes,
  reasonWorded,
  type PreviewView,
} from "./preview-logic";
import type { CameraPreview } from "./use-camera-preview";

const KNOWN = new Set(Object.keys(en));

export function PreviewBar({ control }: { control: CameraPreview }) {
  const { t } = useLocale();
  const { view, working, refused } = control;
  if (!view)
    return control.loaded ? (
      <p className="rg-muted" data-testid="marvel0-preview-unreadable">
        {t("marvel0.preview.unreadable")}
      </p>
    ) : null;
  const holding = holdsCameras(view.state);
  const blocked = view.state === "refused" || view.state === "failed";
  const status = statusOf(view, t);
  return (
    <div
      className="rg-preview"
      data-testid="marvel0-preview"
      data-state={view.state}
    >
      <div className="rg-preview__row">
        <Button
          variant="secondary"
          icon={holding ? CameraOff : Camera}
          // (while it stops the button is busy, not natively disabled: a disabled
          // button drops the keyboard focus, and the same element goes back to
          // "Open" a moment later)
          loading={working !== null || view.state === "stopping"}
          disabled={!holding && !view.mayStart}
          aria-describedby={
            blocked && view.reason ? "marvel0-preview-reason" : undefined
          }
          data-testid={
            holding ? "marvel0-preview-stop" : "marvel0-preview-open"
          }
          onClick={() => void (holding ? control.stop() : control.open())}
        >
          {holding ? t("marvel0.preview.stop") : t("marvel0.preview.open")}
        </Button>
        {status && (
          <span
            className="rg-muted"
            role="status"
            data-testid="marvel0-preview-status"
          >
            {status}
          </span>
        )}
      </div>
      {blocked && view.reason && (
        <p
          className="rg-block"
          id="marvel0-preview-reason"
          data-testid="marvel0-preview-reason"
          data-code={view.reason.code}
        >
          <WordedText worded={reasonWorded(view.reason, KNOWN)} />
        </p>
      )}
      {refused && (
        <RefusedBlock testId="marvel0-preview-refused">
          <WordedText worded={refused} />
        </RefusedBlock>
      )}
      {view.state === "off" &&
        view.stoppedBy !== null &&
        view.stoppedBy !== "requested" && (
          <p className="rg-muted" data-testid="marvel0-preview-stopped">
            {view.stoppedBy === "max_lifetime"
              ? sayText(
                  {
                    key: "marvel0.preview.stopped.max_lifetime",
                    params: { minutes: maxMinutes(view) },
                  },
                  t,
                )
              : wordOf(t, "marvel0.preview.stopped.", view.stoppedBy)}
          </p>
        )}
    </div>
  );
}

/** What the preview is doing, in a sentence; nothing for a refusal or a
 * failure (their reason is shown instead). */
function statusOf(view: PreviewView, t: (key: string) => string): string {
  switch (view.state) {
    case "off":
      return t("marvel0.preview.status.off");
    case "starting":
      return t("marvel0.preview.status.starting");
    case "stopping":
      return t("marvel0.preview.status.stopping");
    case "on":
      return sayText(
        {
          key: "marvel0.preview.status.on",
          params: {
            idle: Math.round(view.limits.idleS),
            minutes: maxMinutes(view),
          },
        },
        t,
      );
    default:
      return "";
  }
}
