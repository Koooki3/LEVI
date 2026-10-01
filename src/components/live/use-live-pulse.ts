"use client";
import { useMemo, useSyncExternalStore } from "react";
import { livePulse, type Pulse } from "./live-logic";
import { pulseStore, type PulseState } from "./live-pulse-store";

const SERVER: PulseState = {
  enabled: null,
  status: null,
  failures: 0,
  at: null,
};

/** `enabled`: true in the live annotation workspace, false in any other,
 * null while that is not known yet (nothing is shown for it either way). */
export function useLivePulse(): { enabled: boolean | null; pulse: Pulse } {
  const state = useSyncExternalStore(
    (listener) => pulseStore().subscribe(listener),
    () => pulseStore().getSnapshot(),
    () => SERVER,
  );
  const pulse = useMemo(
    () => livePulse(state.status, state.failures, (state.at ?? 0) / 1000),
    [state],
  );
  return { enabled: state.enabled, pulse };
}
