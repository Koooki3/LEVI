// Reset data in the export panel: forward demonstrations reversed in time,
// instruction "Reset: <task>" (levi/pool/reset/). Pure functions, no React.
import {
  RESET_DIRECTIONS,
  type ExportFormat,
  type ReleaseClass,
  type ResetDirection,
  type ResetMaxRelease,
  type ResetOnIneligible,
  type ResetOptions,
} from "./types";

export const DEFAULT_TEMPLATE = "Reset: {task}";
export const DEFAULT_CONTRACT = "fr3-robotiq@1";
export const DEFAULT_RELEASE_CAMERA = "observation.images.hand";

/** What the reset form holds (strings as typed; ``resetPayload`` converts). */
export interface ResetState {
  direction: ResetDirection;
  taskTemplate: string;
  actionContract: string;
  maxRelease: ResetMaxRelease;
  onIneligible: ResetOnIneligible;
  releaseCamera: string;
  /** Local vision model connection name; empty = none. */
  reviewModel: string;
  /** One ``forward key = recorded key`` per line. */
  bridgesText: string;
}

export const DEFAULT_RESET_STATE: ResetState = {
  direction: "forward_only",
  taskTemplate: DEFAULT_TEMPLATE,
  actionContract: DEFAULT_CONTRACT,
  maxRelease: "in_reach",
  onIneligible: "exclude",
  releaseCamera: DEFAULT_RELEASE_CAMERA,
  reviewModel: "",
  bridgesText: "",
};

export const DIRECTION_LABELS: Record<ResetDirection, string> = {
  forward_only: "Forward only",
  forward_and_reset: "Forward + reset",
  reset_only: "Reset only",
};

export const DIRECTION_HINTS: Record<ResetDirection, string> = {
  forward_only: "The export as it always was: demonstrations as recorded.",
  forward_and_reset:
    "Every demonstration as recorded, and its time-reversed copy as a reset episode.",
  reset_only: "Only the time-reversed copies, as reset episodes.",
};

export const MAX_RELEASE_LABELS: Record<ResetMaxRelease, string> = {
  in_place: "The object did not move",
  in_reach: "The object settled where the fingers still reach",
};

export const MAX_RELEASE_HINTS: Record<ResetMaxRelease, string> = {
  in_place:
    "Only episodes where every release leaves the object where it was held are reversed.",
  in_reach:
    "Also reverses episodes where the object dropped a little; the frames of the drop are cut out.",
};

export const ON_INELIGIBLE_LABELS: Record<ResetOnIneligible, string> = {
  exclude: "Leave the episode out",
  partial: "Reverse the part before the last safe hold",
};

export const ON_INELIGIBLE_HINTS: Record<ResetOnIneligible, string> = {
  exclude: "An episode that cannot be reversed whole is not exported as reset.",
  partial:
    "The reset starts with the object in the gripper and is marked partial.",
};

/** The reset options for ``POST pool/export``; ``undefined`` (nothing sent)
 * for forward only, or for a format that has no reset (RECAP value, raw
 * capture). Lines of the bridge list that are not ``a = b`` are skipped
 * here; the form refuses them before this is sent. */
export function resetPayload(
  state: ResetState,
  format: ExportFormat,
): ResetOptions | undefined {
  if (format !== "lerobot_v21" || state.direction === "forward_only")
    return undefined;
  return buildOptions(state);
}

/** The same options for the reversibility check, which needs a reset
 * direction even while the export is still forward only. */
export function analysisPayload(state: ResetState): ResetOptions {
  return buildOptions({
    ...state,
    direction:
      state.direction === "forward_only" ? "reset_only" : state.direction,
  });
}

function buildOptions(state: ResetState): ResetOptions {
  const camera = state.releaseCamera.trim();
  const model = state.reviewModel.trim();
  const { bridges } = parseBridges(state.bridgesText);
  return {
    direction: state.direction,
    task_template: state.taskTemplate,
    action_contract: state.actionContract.trim() || DEFAULT_CONTRACT,
    max_release: state.maxRelease,
    on_ineligible: state.onIneligible,
    ...(camera && camera !== DEFAULT_RELEASE_CAMERA
      ? { release_camera: camera }
      : {}),
    ...(model ? { review_model: model } : {}),
    ...(bridges.length ? { bridges } : {}),
  };
}

export function isResetDirection(value: string): value is ResetDirection {
  return (RESET_DIRECTIONS as string[]).includes(value);
}

/** Why the instruction template cannot be used (English key for t()), or
 * null. It must hold ``{task}`` exactly once and no other brace. */
export function templateProblem(template: string): string | null {
  const count = template.split("{task}").length - 1;
  if (count === 0) return "The template must contain {task}.";
  if (count > 1) return "Use {task} only once in the template.";
  if (template.replace("{task}", "").match(/[{}]/))
    return "Only {task} may appear in braces.";
  if (template.length > 400)
    return "The template is too long (400 characters).";
  return null;
}

/** The reset instruction for a task text ("" while the template is invalid). */
export function previewText(template: string, task: string): string {
  return templateProblem(template)
    ? ""
    : template.replace("{task}", () => task);
}

export interface BridgeParse {
  bridges: { source: string; record: string }[];
  /** 1-based numbers of the lines that are not ``forward key = recorded key``. */
  invalid: number[];
}

/** ``forward key = recorded key``, one per line; blank lines and ``#`` lines
 * are ignored; the same forward key twice keeps the last. */
export function parseBridges(text: string): BridgeParse {
  const byKey = new Map<string, string>();
  const invalid: number[] = [];
  text.split("\n").forEach((raw, index) => {
    const line = raw.trim();
    if (!line || line.startsWith("#")) return;
    const cut = line.indexOf("=");
    const source = cut < 0 ? "" : line.slice(0, cut).trim();
    const record = cut < 0 ? "" : line.slice(cut + 1).trim();
    if (!source || !record) invalid.push(index + 1);
    else byKey.set(source, record);
  });
  return {
    bridges: [...byKey].map(([source, record]) => ({ source, record })),
    invalid,
  };
}

/** The end of a long episode key (its last path parts), for a table cell. */
export function shortKey(key: string, max = 40): string {
  const parts = key.split("/").filter(Boolean);
  const tail = parts.slice(-2).join("/") || key;
  return tail.length <= max ? tail : `…${tail.slice(tail.length - max + 1)}`;
}

/** Release classes, best first (the order they are listed in). */
export const RELEASE_CLASS_ORDER: ReleaseClass[] = [
  "in_place",
  "in_reach",
  "escaped",
  "unknown",
];

export const RELEASE_CLASS_LABELS: Record<ReleaseClass, string> = {
  in_place: "Stayed put",
  in_reach: "Within reach",
  escaped: "Out of reach",
  unknown: "Not judged",
};

export const RELEASE_CLASS_HINTS: Record<ReleaseClass, string> = {
  in_place: "The object stayed where it was held when the gripper opened.",
  in_reach:
    "The object settled where the fingers can still reach it; the frames of its fall are cut out.",
  escaped: "The object ended where the fingers cannot reach it.",
  unknown: "The images were not enough to tell where the object went.",
};

export type ClassTone = "success" | "info" | "danger" | "warning";

export function releaseTone(value: ReleaseClass): ClassTone {
  switch (value) {
    case "in_place":
      return "success";
    case "in_reach":
      return "info";
    case "escaped":
      return "danger";
    default:
      return "warning";
  }
}

export function releaseClassLabel(value: string): string {
  return RELEASE_CLASS_LABELS[value as ReleaseClass] ?? value;
}

/** Counts per release class in listing order; classes with none are left out. */
export function sortedClasses(
  counts: Partial<Record<ReleaseClass, number>>,
): [ReleaseClass, number][] {
  return RELEASE_CLASS_ORDER.filter((c) => (counts[c] ?? 0) > 0).map((c) => [
    c,
    counts[c] as number,
  ]);
}

/** Why a release was judged as it was (levi/pool/reset/analysis.py). */
export const RELEASE_REASON_LABELS: Record<string, string> = {
  no_hold_frame: "no frame with the object held",
  arm_left_before_settle: "the arm left before the object settled",
  no_release_camera: "no video from the release camera",
  object_still_moving: "the object was still moving",
  ambiguous_match: "the object could not be matched in the image",
  low_texture: "too little texture to follow the object",
  object_left_the_fingers: "the object left the fingers' reach",
  vlm_veto: "the vision model did not accept it",
  event_in_seam: "the release falls on the splice",
  release_camera_unreadable: "the release camera video could not be read",
};

/** A release reason as a key for t(); the free-text
 * ``release_camera_unreadable…`` variants map to one label, an unknown code
 * is shown as it is. */
export function releaseReasonLabel(reason: string): string {
  if (reason.startsWith("release_camera_unreadable"))
    return RELEASE_REASON_LABELS.release_camera_unreadable;
  return RELEASE_REASON_LABELS[reason] ?? reason;
}

/** Excluded-reason codes of a reset export. */
export function isResetReason(code: string): boolean {
  return code.startsWith("reset_");
}

/** Reasons by count (largest first, then by code), as ``[code, n]``. */
export function sortedReasons(
  reasons: Record<string, number>,
): [string, number][] {
  return Object.entries(reasons).sort(
    (a, b) => b[1] - a[1] || a[0].localeCompare(b[0]),
  );
}

/** Episodes that need a recorded stretch (the export writes
 * ``meta/levi_reset_capture_requests.json`` for them). */
export function needsCapture(excluded: Record<string, number> | undefined) {
  return (
    (excluded?.reset_release_escaped ?? 0) +
      (excluded?.reset_release_unknown ?? 0) >
    0
  );
}
