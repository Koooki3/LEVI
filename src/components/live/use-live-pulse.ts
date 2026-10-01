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

// Stable identities: a new subscribe function on every render would make React
// unsubscribe and subscribe again (and the store would ask again).
const subscribe = (listener: () => void) => pulseStore().subscribe(listener);
const snapshot = () => pulseStore().getSnapshot();
const server = () => SERVER;

/** `enabled`: true in the live annotation workspace, false in any other,
 * null while that is not known yet (nothing is shown for it either way). */
export function useLivePulse(): { enabled: boolean | null; pulse: Pulse } {
  const state = useSyncExternalStore(subscribe, snapshot, server);
  const pulse = useMemo(
    () => livePulse(state.status, state.failures, (state.at ?? 0) / 1000),
    [state],
  );
  return { enabled: state.enabled, pulse };
}
