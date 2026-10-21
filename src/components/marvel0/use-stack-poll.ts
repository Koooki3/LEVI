"use client";
// The arm stack document, read every 2 s while the tab is visible
// (use-polled.ts has the rules). One answer feeds two views: the passive
// card of the S4-0 module (`stackView`: evidence, gate dry runs) and the
// controls (`actView`: buttons, blockers, readiness).
import { useCallback } from "react";
import en from "@/i18n/en.json";
import { actView, type ActView } from "./stack-act-logic";
import type { StackApi } from "./stack-api";
import { STACK_POLL_MS, stackView, type StackView } from "./stack-status-logic";
import { usePolled, type Polled } from "./use-polled";

/** An answer older than this is not shown as current (three missed reads). */
export const STACK_FRESH_MS = 8_000;

const KNOWN = new Set(Object.keys(en));

export interface StackDoc {
  passive: StackView;
  /** Null: this backend has no controls (or the answer lacks them). */
  act: ActView | null;
}

export function parseStack(data: unknown): StackDoc | null {
  const passive = stackView(data, KNOWN);
  return passive ? { passive, act: actView(data, KNOWN, passive) } : null;
}

export function useStackPoll(
  api: StackApi,
  { pollMs = STACK_POLL_MS }: { pollMs?: number } = {},
): Polled<StackDoc> {
  const read = useCallback((signal: AbortSignal) => api.read(signal), [api]);
  return usePolled<StackDoc>({
    read,
    parse: parseStack,
    pollMs,
    freshMs: STACK_FRESH_MS,
  });
}
