"use client";
// The one selected model of the marvel0 page. The policy server card is where
// a model is chosen (with RTC and the guidance strength); the command card and
// the run card follow it and have no model control of their own. While this
// page's policy server is starting or running, what it runs is the selection
// whatever was chosen before, so the command can never ask for another model
// than the server serves.
//
// The rules are pure functions (`effectiveSelection`); `useChoice` only keeps
// what the person picked.
import { useCallback, useMemo, useState } from "react";
import { inferenceMode, type InferenceMode } from "./inference-mode";
import type { ModelsView } from "./catalog-logic";
import type { DeployStatus } from "./deploy-logic";

/** What the person picked in the policy server card; null is "not picked". */
export interface Choice {
  modelId: string | null;
  rtc: boolean | null;
  inferenceMode?: InferenceMode;
  vlashOverlapSteps?: string;
  /** The guidance strength as typed; empty is the server's default. */
  scale: string;
}
export const NO_CHOICE: Choice = { modelId: null, rtc: null, scale: "" };

export type SelectionSource = "server" | "choice" | "default";

export interface Effective {
  modelId: string;
  rtc: boolean;
  inferenceMode?: InferenceMode;
  vlashOverlapSteps?: string;
  /** The guidance strength the running server uses; null: none or not known. */
  cfgScale: number | null;
  /** Where the model comes from: the server this page runs, the person's
   * pick in the policy server card, or the first model of the list. */
  source: SelectionSource;
}

/** The states in which this page's policy server holds a model. */
const HOLDING: readonly string[] = ["starting", "ready", "stopping"];

/** The policy server of this page that holds a model, or null. A server
 * started by hand (`foreign_listener`) has no model this page knows. */
export function serverHolding(status: DeployStatus | null) {
  if (!status || !HOLDING.includes(status.state)) return null;
  const running = status.deploy;
  return running && running.modelId ? running : null;
}

/** The selection the page works with: the running server's, else the
 * person's pick, else the first model; null before the model list is read. */
export function effectiveSelection(
  models: ModelsView | null,
  choice: Choice,
  status: DeployStatus | null,
): Effective | null {
  if (!models || models.models.length === 0) return null;
  const running = serverHolding(status);
  if (running && models.models.some((m) => m.id === running.modelId))
    return {
      modelId: running.modelId!,
      rtc: inferenceMode(running.inferenceMode, running.rtc) === "rtc",
      inferenceMode: inferenceMode(running.inferenceMode, running.rtc),
      vlashOverlapSteps: String(running.vlashOverlapSteps ?? 2),
      cfgScale: running.cfgScale,
      source: "server",
    };
  const picked = models.models.find((m) => m.id === choice.modelId);
  return {
    modelId: (picked ?? models.models[0]).id,
    rtc:
      inferenceMode(choice.inferenceMode, choice.rtc ?? models.defaults.rtc) ===
      "rtc",
    inferenceMode: inferenceMode(
      choice.inferenceMode,
      choice.rtc ?? models.defaults.rtc,
    ),
    vlashOverlapSteps: choice.vlashOverlapSteps ?? "2",
    cfgScale: null,
    source: picked ? "choice" : "default",
  };
}

export interface ChoiceState {
  choice: Choice;
  /** Changes what is picked. */
  pick: (change: Partial<Choice>) => void;
}

/** The person's pick, kept by the component that owns the policy server and
 * command cards. */
export function useChoice(): ChoiceState {
  const [choice, setChoice] = useState<Choice>(NO_CHOICE);
  const pick = useCallback(
    (change: Partial<Choice>) => setChoice((now) => ({ ...now, ...change })),
    [],
  );
  return useMemo(() => ({ choice, pick }), [choice, pick]);
}
