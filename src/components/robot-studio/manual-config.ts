import type { ManualLimits, ManualStopLimits } from "./manual-api";
import { validManualLimits, validManualStopLimits } from "./manual-session";
import { isValidWorkspaceBox } from "./workspace-draft";

export type ManualConfiguration = {
  schema: "levi.robot-studio.manual-config.v1";
  limits: ManualLimits;
  stop_limits: ManualStopLimits;
};

const object = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value);
const LIMIT_KEYS = [
  "box_min_m",
  "box_max_m",
  "max_delta_m",
  "max_step_m",
  "max_velocity_m_s",
  "period_s",
  "max_reading_age_s",
  "max_anchor_age_s",
  "deadman_timeout_s",
  "target_timeout_s",
  "max_orientation_drift_rad",
  "quaternion_norm_tolerance",
  "max_settle_age_s",
];
const STOP_KEYS = [
  "settle_window_s",
  "settle_position_m",
  "settle_angle_rad",
  "gripper_settle_rad",
];
const exactKeys = (value: Record<string, unknown>, keys: string[]) =>
  Object.keys(value).length === keys.length &&
  keys.every((key) => Object.prototype.hasOwnProperty.call(value, key));

/** A configuration contains numeric drafts only, never permissions or attestations. */
export function parseManualConfiguration(
  text: string,
): ManualConfiguration | null {
  try {
    if (text.length > 16384) return null;
    const value: unknown = JSON.parse(text);
    if (
      !object(value) ||
      !exactKeys(value, ["schema", "limits", "stop_limits"]) ||
      value.schema !== "levi.robot-studio.manual-config.v1" ||
      !object(value.limits) ||
      !exactKeys(value.limits, LIMIT_KEYS) ||
      !object(value.stop_limits) ||
      !exactKeys(value.stop_limits, STOP_KEYS)
    )
      return null;
    const limits = value.limits as unknown as ManualLimits;
    const stops = value.stop_limits as unknown as ManualStopLimits;
    if (
      !validManualLimits(limits) ||
      !validManualStopLimits(stops) ||
      !isValidWorkspaceBox({ min: limits.box_min_m, max: limits.box_max_m })
    )
      return null;
    return {
      schema: value.schema,
      limits: {
        ...limits,
        box_min_m: [...limits.box_min_m],
        box_max_m: [...limits.box_max_m],
      },
      stop_limits: { ...stops },
    };
  } catch {
    return null;
  }
}

export function exportManualConfiguration(
  limits: ManualLimits,
  stops: ManualStopLimits,
): string {
  const text = JSON.stringify(
    {
      schema: "levi.robot-studio.manual-config.v1",
      limits,
      stop_limits: stops,
    },
    null,
    2,
  );
  if (!parseManualConfiguration(text))
    throw new RangeError("Invalid manual configuration");
  return text + "\n";
}

/** Reveal folded configuration without changing a draft or starting a session. */
export function revealManualConfiguration(id: string): void {
  const section = document.getElementById(id);
  if (!section) return;
  for (
    let node: HTMLElement | null = section;
    node;
    node = node.parentElement
  ) {
    if (node instanceof HTMLDetailsElement) node.open = true;
  }
  section.querySelectorAll("details").forEach((details) => {
    details.open = true;
  });
  section.scrollIntoView?.({ block: "center", behavior: "smooth" });
  const target = section.matches("button,input,textarea")
    ? section
    : section.querySelector<HTMLElement>(
        "input:not(:disabled),textarea:not(:disabled),button:not(:disabled)",
      );
  target?.focus({ preventScroll: true });
}
