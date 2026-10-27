import { describe, expect, test } from "bun:test";
import {
  MIC_CHOICE_KEY,
  MIC_SEEN_KEY,
  UNKNOWN_LIST,
  micKey,
  micList,
  micNews,
  readMicChoice,
  readSeenMics,
  resolveMic,
  writeMicChoice,
  writeSeenMics,
  type MicList,
  type StorageLike,
} from "../mic-choice";

const UGREEN = { id: "ugreen-1", label: "UGREEN USB MIC-CM727 Analog Stereo" };
const CAMERA = { id: "insta-1", label: "Insta360 Ace Pro 2 Analog Stereo" };
const input = (deviceId: string, label: string, kind = "audioinput") => ({
  deviceId,
  label,
  kind,
});
const listOf = (...inputs: Array<{ id: string; label: string }>): MicList => ({
  known: true,
  inputs,
  hidden: false,
});

function memoryStorage(initial: Record<string, string> = {}) {
  const data = new Map(Object.entries(initial));
  const storage: StorageLike = {
    getItem: (key) => data.get(key) ?? null,
    setItem: (key, value) => void data.set(key, value),
    removeItem: (key) => void data.delete(key),
  };
  return { storage, data };
}
const throwing: StorageLike = {
  getItem: () => {
    throw new Error("SecurityError");
  },
  setItem: () => {
    throw new Error("QuotaExceededError");
  },
  removeItem: () => {
    throw new Error("SecurityError");
  },
};

describe("micList", () => {
  test("keeps audio inputs in order, without outputs, cameras, the default/communications aliases or duplicates", () => {
    const list = micList([
      input("default", "Default - UGREEN USB MIC-CM727 Analog Stereo"),
      input("communications", "Communications - Insta360"),
      input(UGREEN.id, UGREEN.label),
      input("speaker-1", "HDMI", "audiooutput"),
      input("cam-1", "Camera", "videoinput"),
      input(CAMERA.id, `  ${CAMERA.label}  `),
      input(UGREEN.id, UGREEN.label),
    ]);
    expect(list).toEqual({
      known: true,
      inputs: [UGREEN, CAMERA],
      hidden: false,
    });
  });

  test("names or ids kept back until the microphone is allowed make the list hidden", () => {
    // Chromium before permission: one entry per kind, empty id and name.
    expect(micList([input("", "")])).toEqual({
      known: true,
      inputs: [],
      hidden: true,
    });
    // Ids but no name at all (Firefox before permission): listed, unnamed.
    expect(micList([input("abc", "")])).toEqual({
      known: true,
      inputs: [{ id: "abc", label: "" }],
      hidden: true,
    });
  });

  test("one unnamed virtual source among named inputs does not hide the list", () => {
    expect(
      micList([input(UGREEN.id, UGREEN.label), input("virtual-1", "")]),
    ).toEqual({
      known: true,
      inputs: [UGREEN, { id: "virtual-1", label: "" }],
      hidden: false,
    });
  });

  test("an id too long to be a device's is dropped", () => {
    expect(micList([input("x".repeat(600), "odd")]).inputs).toEqual([]);
  });
});

describe("the remembered choice", () => {
  test("is written and read back as id and name", () => {
    const { storage, data } = memoryStorage();
    expect(writeMicChoice(storage, UGREEN)).toBe(true);
    expect(JSON.parse(data.get(MIC_CHOICE_KEY)!)).toEqual(UGREEN);
    expect(readMicChoice(storage)).toEqual(UGREEN);
    expect(writeMicChoice(storage, null)).toBe(true);
    expect(data.has(MIC_CHOICE_KEY)).toBe(false);
    expect(readMicChoice(storage)).toBeNull();
  });

  test("garbage, a wrong shape or an alias id reads as no choice", () => {
    for (const raw of [
      "not json",
      "null",
      "[]",
      '"ugreen"',
      '{"id":3,"label":"x"}',
      '{"id":"","label":"x"}',
      '{"id":"default","label":"x"}',
      '{"id":"a"}',
      JSON.stringify({ id: "a", label: "x".repeat(600) }),
    ])
      expect(
        readMicChoice(memoryStorage({ [MIC_CHOICE_KEY]: raw }).storage),
        raw,
      ).toBeNull();
  });

  test("a storage that throws, or none at all, never throws here", () => {
    expect(readMicChoice(throwing)).toBeNull();
    expect(writeMicChoice(throwing, UGREEN)).toBe(false);
    expect(writeMicChoice(throwing, null)).toBe(false);
    expect(readMicChoice(null)).toBeNull();
    expect(writeMicChoice(null, UGREEN)).toBe(false);
  });
});

describe("resolveMic", () => {
  test("no choice is the system default", () => {
    expect(resolveMic(null, listOf(UGREEN, CAMERA))).toEqual({
      deviceId: null,
      missing: null,
      renamed: null,
    });
  });

  test("a connected choice is opened by its id", () => {
    expect(resolveMic(UGREEN, listOf(CAMERA, UGREEN)).deviceId).toBe(UGREEN.id);
  });

  test("before the list is known, or while it is all hidden, the remembered id is tried", () => {
    expect(resolveMic(UGREEN, UNKNOWN_LIST)).toEqual({
      deviceId: UGREEN.id,
      missing: null,
      renamed: null,
    });
    expect(
      resolveMic(UGREEN, { known: true, inputs: [], hidden: true }).deviceId,
    ).toBe(UGREEN.id);
  });

  test("a choice that is not connected is reported missing and the default is used", () => {
    expect(resolveMic(UGREEN, listOf(CAMERA))).toEqual({
      deviceId: null,
      missing: UGREEN,
      renamed: null,
    });
    expect(resolveMic(UGREEN, listOf()).missing).toEqual(UGREEN);
  });

  test("a new id with the same name, carried by exactly one input, is the same microphone", () => {
    const moved = { id: "ugreen-2", label: UGREEN.label };
    expect(resolveMic(UGREEN, listOf(CAMERA, moved))).toEqual({
      deviceId: "ugreen-2",
      missing: null,
      renamed: moved,
    });
    // Two inputs with that name: which one is not known, so it is missing.
    const twin = { id: "ugreen-3", label: UGREEN.label };
    expect(resolveMic(UGREEN, listOf(moved, twin))).toEqual({
      deviceId: null,
      missing: UGREEN,
      renamed: null,
    });
    // A choice without a name is never matched by name.
    expect(
      resolveMic({ id: "gone", label: "" }, listOf({ id: "x", label: "" }))
        .missing,
    ).toEqual({ id: "gone", label: "" });
  });
});

describe("new microphones", () => {
  const NEWMIC = { id: "rode-1", label: "RODE NT-USB Mini" };
  const seenOf = (...inputs: Array<{ label: string }>) =>
    inputs.map((input) => `name:${input.label}`);

  test("an input is recognised by its name; one without a name cannot be", () => {
    expect(micKey(UGREEN)).toBe(`name:${UGREEN.label}`);
    expect(micKey({ id: "x", label: "" })).toBeNull();
  });

  test("the seen list is stored, read back, capped, and survives bad storage", () => {
    const { storage, data } = memoryStorage();
    expect(readSeenMics(storage)).toBeNull();
    expect(writeSeenMics(storage, seenOf(UGREEN, CAMERA))).toBe(true);
    expect(readSeenMics(storage)).toEqual(seenOf(UGREEN, CAMERA));
    const many = Array.from({ length: 80 }, (_, i) => `name:mic ${i}`);
    writeSeenMics(storage, many);
    expect(readSeenMics(storage)).toEqual(many.slice(-64));
    data.set(MIC_SEEN_KEY, JSON.stringify(["name:ok", 3, "id:x", ""]));
    expect(readSeenMics(storage)).toEqual(["name:ok"]);
    data.set(MIC_SEEN_KEY, "{not json");
    expect(readSeenMics(storage)).toBeNull();
    expect(readSeenMics(throwing)).toBeNull();
    expect(writeSeenMics(throwing, ["name:a"])).toBe(false);
  });

  test("a list that keeps names back, or is not read yet, tells nothing and stores nothing", () => {
    expect(micNews(UNKNOWN_LIST, null, null)).toEqual({
      news: { kind: "none" },
      seen: null,
    });
    expect(
      micNews(
        { known: true, inputs: [], hidden: true },
        seenOf(UGREEN),
        UGREEN,
      ),
    ).toEqual({ news: { kind: "none" }, seen: null });
  });

  test("the first list ever is recorded without offering anything", () => {
    expect(micNews(listOf(CAMERA, UGREEN), null, null)).toEqual({
      news: { kind: "first" },
      seen: seenOf(CAMERA, UGREEN),
    });
  });

  test("nothing new: the connected inputs move to the end of the seen list", () => {
    const old = { label: "Old headset" };
    expect(
      micNews(listOf(UGREEN), seenOf(UGREEN, old, CAMERA), UGREEN),
    ).toEqual({
      news: { kind: "none" },
      seen: seenOf(old, CAMERA, UGREEN),
    });
  });

  test("ids that changed between sessions do not make a known microphone new", () => {
    const moved = { id: "ugreen-9", label: UGREEN.label };
    expect(
      micNews(listOf(CAMERA, moved), seenOf(CAMERA, UGREEN), null).news,
    ).toEqual({ kind: "none" });
  });

  test("a swap: the chosen microphone is gone and exactly one new one appeared", () => {
    expect(
      micNews(listOf(CAMERA, NEWMIC), seenOf(CAMERA, UGREEN), UGREEN),
    ).toEqual({
      news: { kind: "swap", to: NEWMIC, from: UGREEN },
      seen: seenOf(UGREEN, CAMERA, NEWMIC),
    });
  });

  test("a new microphone is only offered while the chosen one is connected, or nothing was chosen", () => {
    const seen = seenOf(CAMERA, UGREEN);
    expect(micNews(listOf(CAMERA, UGREEN, NEWMIC), seen, UGREEN).news).toEqual({
      kind: "offer",
      inputs: [NEWMIC],
    });
    expect(micNews(listOf(CAMERA, UGREEN, NEWMIC), seen, null).news).toEqual({
      kind: "offer",
      inputs: [NEWMIC],
    });
    // The chosen one found again under a new id is not gone.
    const moved = { id: "ugreen-9", label: UGREEN.label };
    expect(micNews(listOf(moved, NEWMIC), seen, UGREEN).news).toEqual({
      kind: "offer",
      inputs: [NEWMIC],
    });
  });

  test("several new microphones are offered, never switched to", () => {
    const other = { id: "cam-mic-2", label: "USB Camera Analog Stereo" };
    expect(
      micNews(listOf(CAMERA, NEWMIC, other), seenOf(CAMERA, UGREEN), UGREEN)
        .news,
    ).toEqual({ kind: "offer", inputs: [NEWMIC, other] });
  });

  test("an input without a name is never new, so never switched to", () => {
    const virtual = { id: "virtual-7", label: "" };
    expect(
      micNews(listOf(CAMERA, virtual), seenOf(CAMERA, UGREEN), UGREEN),
    ).toEqual({ news: { kind: "none" }, seen: seenOf(UGREEN, CAMERA) });
  });
});
