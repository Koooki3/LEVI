"use client";
// The camera preview's state and its two buttons for the Run card's live view.
// The document is read once a second while the card is shown and no client of
// the page is on (use-polled.ts has the rules: nothing while the tab is hidden,
// a failed read doubles the wait); Open and Stop send one request each and read
// again. A refusal is worded from the catalogue (`marvel0.preview.reason.`),
// else by the generic refusals of the other cards. When a client is on, nothing
// is read and nothing is shown: the backend has already stopped the preview,
// because every start of a client takes the cameras back first.
import { useCallback, useEffect, useRef, useState } from "react";
import en from "@/i18n/en.json";
import { sayRefusal } from "./ctl-failure";
import type { Worded } from "./marvel0-json";
import type { Say } from "./marvel0-logic";
import type { PreviewApi } from "./preview-api";
import { previewView, REASON_PREFIX, type PreviewView } from "./preview-logic";
import { usePolled } from "./use-polled";

export const PREVIEW_POLL_MS = 1_000;
/** An answer older than this is not shown as current. */
export const PREVIEW_FRESH_MS = 5_000;

const KNOWN = new Set(Object.keys(en));

export interface CameraPreview {
  /** The document last read, null until one was (or when it is too old). */
  view: PreviewView | null;
  /** The first read has finished. */
  loaded: boolean;
  /** The request in flight, if any. */
  working: "start" | "stop" | null;
  /** What the backend said to the last Open or Stop it refused. */
  refused: Worded<Say> | null;
  open: () => Promise<void>;
  stop: () => Promise<void>;
}

export function useCameraPreview(
  api: PreviewApi | undefined,
  enabled: boolean,
  { pollMs = PREVIEW_POLL_MS }: { pollMs?: number } = {},
): CameraPreview {
  const on = enabled && api !== undefined;
  const read = useCallback(
    (signal: AbortSignal) =>
      api
        ? api.read(signal)
        : Promise.resolve({
            ok: false as const,
            status: 0,
            code: "network",
            message: "",
            extra: {},
            field: null,
            fields: [],
          }),
    [api],
  );
  const polled = usePolled<PreviewView>({
    read,
    parse: previewView,
    pollMs,
    freshMs: PREVIEW_FRESH_MS,
    enabled: on,
    // A run came and went: the document from before it is not the state now
    // (the run's start took the cameras back), so nothing is shown, and no
    // picture is asked for, until the first read after the run has answered.
    forgetWhenDisabled: true,
  });
  const busy = useRef(false);
  const [working, setWorking] = useState<"start" | "stop" | null>(null);
  const [refused, setRefused] = useState<Worded<Say> | null>(null);

  // A client is on (or the card is gone): forget the last refusal.
  useEffect(() => {
    if (!on) setRefused(null);
  }, [on]);

  const send = useCallback(
    async (kind: "start" | "stop") => {
      if (!api || busy.current) return;
      busy.current = true;
      setWorking(kind);
      setRefused(null);
      try {
        const answer = await (kind === "start" ? api.start() : api.stop());
        if (!answer.ok) setRefused(sayRefusal(answer, REASON_PREFIX, KNOWN));
        await polled.refresh();
      } finally {
        busy.current = false;
        setWorking(null);
      }
    },
    [api, polled],
  );

  return {
    // (use-polled.ts drops an answer once it is older than PREVIEW_FRESH_MS, so
    // a failed read does not take Stop away at once from a preview that holds
    // the cameras)
    view: on ? polled.value : null,
    loaded: on && polled.loaded,
    working,
    refused,
    open: () => send("start"),
    stop: () => send("stop"),
  };
}
