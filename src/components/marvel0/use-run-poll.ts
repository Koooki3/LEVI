"use client";
// The hosted client's document, read every second while the tab is visible
// (use-polled.ts has the rules). It keeps the state last seen even when a read
// fails, because the HOLD button must stay on for a client that was running
// when the answers stopped (`lastSeen`).
import { useCallback, useRef } from "react";
import en from "@/i18n/en.json";
import type { RunApi } from "./run-api";
import { runView, type RunView, type Seen } from "./run-logic";
import { usePolled, type Polled } from "./use-polled";

export const RUN_POLL_MS = 1_000;
/** An answer older than this is not shown as current. */
export const RUN_FRESH_MS = 5_000;

const KNOWN = new Set(Object.keys(en));

export interface RunPoll extends Polled<RunView> {
  /** The state of the last good answer, kept however old it is; null until
   * one was read. */
  lastSeen: Seen | null;
}

export function useRunPoll(
  api: RunApi,
  { pollMs = RUN_POLL_MS }: { pollMs?: number } = {},
): RunPoll {
  const seen = useRef<Seen | null>(null);
  const read = useCallback((signal: AbortSignal) => api.read(signal), [api]);
  const parse = useCallback((data: unknown) => {
    const view = runView(data, KNOWN);
    if (view) seen.current = { state: view.state, mayMove: view.mayMove };
    return view;
  }, []);
  const polled = usePolled<RunView>({
    read,
    parse,
    pollMs,
    freshMs: RUN_FRESH_MS,
  });
  return { ...polled, lastSeen: seen.current };
}
