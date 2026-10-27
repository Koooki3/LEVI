// Which microphone the voice card records from. A machine often has several
// inputs (a dedicated USB microphone, a camera's microphone, the board's jack)
// and the system default is not always the one the person talks into, so the
// card lists the inputs and remembers a choice.
//
//   * The list comes from `enumerateDevices`, which opens nothing and asks
//     nothing. Until the page has been allowed the microphone once, a browser
//     hides the names (and Chromium the ids too): the list is then "hidden" and
//     only the system default is offered, with a hint.
//   * The choice lives in this browser's localStorage only (id and name; no
//     audio, nothing sent to LEVI). Every read and write may fail (a private
//     window, blocked site data): the card then simply uses the default.
//   * A remembered input that is not connected is not forgotten: the default
//     is used and the card says so, and the input is used again once it is
//     back. An id that changed (a browser may rotate ids between sessions) is
//     found again by its name when exactly one input carries it.

/** The part of MediaDeviceInfo read here. */
export interface DeviceInfoLike {
  readonly deviceId: string;
  readonly kind: string;
  readonly label: string;
}

export interface MicInput {
  id: string;
  label: string;
}

export interface MicList {
  /** `enumerateDevices` answered at least once. */
  known: boolean;
  inputs: MicInput[];
  /** Inputs exist whose names (or ids) the browser keeps back until the
   * microphone is allowed. */
  hidden: boolean;
}

export const UNKNOWN_LIST: MicList = {
  known: false,
  inputs: [],
  hidden: false,
};

/** The remembered choice: an input's id and the name it had. */
export interface MicChoice {
  id: string;
  label: string;
}

export const MIC_CHOICE_KEY = "levi.marvel0.speech.mic.v1";
/** Ids and names longer than this are not a device's: they are not kept. */
const MAX_FIELD = 512;
// Chromium lists the system default and the communications device again
// under these ids; "System default" is offered separately.
const PSEUDO_IDS = new Set(["default", "communications"]);

/** The audio inputs of an `enumerateDevices` answer, in its order. */
export function micList(devices: readonly DeviceInfoLike[]): MicList {
  const inputs: MicInput[] = [];
  const seen = new Set<string>();
  // Kept back: an input without an id (Chromium before permission), or no
  // input with a name at all. One unnamed virtual source among named ones
  // does not make the whole list hidden.
  let noId = false;
  let anyAudio = false;
  let anyName = false;
  for (const device of devices) {
    if (device.kind !== "audioinput") continue;
    anyAudio = true;
    const id = typeof device.deviceId === "string" ? device.deviceId : "";
    const label = typeof device.label === "string" ? device.label.trim() : "";
    if (!id) noId = true;
    if (label) anyName = true;
    if (!id || PSEUDO_IDS.has(id) || seen.has(id) || id.length > MAX_FIELD)
      continue;
    seen.add(id);
    inputs.push({ id, label: label.slice(0, MAX_FIELD) });
  }
  return { known: true, inputs, hidden: noId || (anyAudio && !anyName) };
}

export type StorageLike = Pick<Storage, "getItem" | "setItem" | "removeItem">;

/** This page's localStorage, or null where reaching it throws. */
export function browserStorage(): StorageLike | null {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}

export function readMicChoice(storage: StorageLike | null): MicChoice | null {
  if (!storage) return null;
  try {
    const raw = storage.getItem(MIC_CHOICE_KEY);
    if (!raw) return null;
    const value = JSON.parse(raw) as unknown;
    if (typeof value !== "object" || value === null) return null;
    const { id, label } = value as Record<string, unknown>;
    if (
      typeof id !== "string" ||
      typeof label !== "string" ||
      !id ||
      PSEUDO_IDS.has(id) ||
      id.length > MAX_FIELD ||
      label.length > MAX_FIELD
    )
      return null;
    return { id, label };
  } catch {
    return null;
  }
}

/** Stores the choice (null: the system default). False when it could not. */
export function writeMicChoice(
  storage: StorageLike | null,
  choice: MicChoice | null,
): boolean {
  if (!storage) return false;
  try {
    if (choice === null) storage.removeItem(MIC_CHOICE_KEY);
    else
      storage.setItem(
        MIC_CHOICE_KEY,
        JSON.stringify({ id: choice.id, label: choice.label }),
      );
    return true;
  } catch {
    return false;
  }
}

export interface MicResolution {
  /** The id to open (null: the system default). */
  deviceId: string | null;
  /** The remembered input that is not connected now. */
  missing: MicChoice | null;
  /** The remembered input was found again under a new id (store it). */
  renamed: MicChoice | null;
}

/** Which input a press opens, given the choice and what is connected. */
export function resolveMic(
  choice: MicChoice | null,
  list: MicList,
): MicResolution {
  const none = { missing: null, renamed: null };
  if (!choice) return { deviceId: null, ...none };
  // Nothing to check against yet: try the remembered id (the capture falls
  // back to the default if it is gone).
  if (!list.known || (list.hidden && list.inputs.length === 0))
    return { deviceId: choice.id, ...none };
  if (list.inputs.some((input) => input.id === choice.id))
    return { deviceId: choice.id, ...none };
  if (choice.label) {
    const same = list.inputs.filter((input) => input.label === choice.label);
    if (same.length === 1)
      return {
        deviceId: same[0].id,
        missing: null,
        renamed: { id: same[0].id, label: choice.label },
      };
  }
  return { deviceId: null, missing: choice, renamed: null };
}
