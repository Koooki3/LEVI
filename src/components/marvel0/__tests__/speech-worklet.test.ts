import { describe, expect, test } from "bun:test";
import { readFileSync } from "fs";
import { join } from "path";

const source = readFileSync(
  join(import.meta.dir, "../../../../public/voice-worklet.js"),
  "utf8",
);

type Posted = {
  message: { pcm: Float32Array; peak: number; rms: number };
  transfer: Transferable[];
};
interface Processor {
  process(inputs: Float32Array[][]): boolean;
}

/** Runs the worklet file against the two globals it uses. */
function load() {
  const posted: Posted[] = [];
  const registered: Record<string, new () => Processor> = {};
  class Base {
    port = {
      postMessage: (message: Posted["message"], transfer: Transferable[]) =>
        void posted.push({ message, transfer }),
    };
  }
  new Function("AudioWorkletProcessor", "registerProcessor", source)(
    Base,
    (name: string, processor: new () => Processor) => {
      registered[name] = processor;
    },
  );
  return { posted, registered };
}

const block = (value: number | ((i: number) => number), frames = 128) =>
  Float32Array.from({ length: frames }, (_, i) =>
    typeof value === "number" ? value : value(i),
  );

describe("public/voice-worklet.js", () => {
  test("registers the processor the page asks for", () => {
    expect(Object.keys(load().registered)).toEqual(["levi-pcm-tap"]);
  });

  test("hands over blocks of 512 mono frames with their peak and RMS, the buffer transferred", () => {
    const { posted, registered } = load();
    const tap = new registered["levi-pcm-tap"]();
    for (let i = 0; i < 4; i += 1)
      expect(tap.process([[block(0.5)]])).toBe(true);
    expect(posted).toHaveLength(1);
    const { message, transfer } = posted[0];
    expect(message.pcm).toHaveLength(512);
    expect(message.peak).toBeCloseTo(0.5, 6);
    expect(message.rms).toBeCloseTo(0.5, 6);
    expect(transfer).toEqual([message.pcm.buffer]);
    tap.process([[block(0.25)]]);
    expect(posted).toHaveLength(1); // the next block is not full yet
  });

  test("keeps its place across calls of any length", () => {
    const { posted, registered } = load();
    const tap = new registered["levi-pcm-tap"]();
    tap.process([[block(0.1, 300)]]);
    expect(posted).toHaveLength(0);
    tap.process([[block(0.1, 300)]]);
    expect(posted).toHaveLength(1);
    tap.process([[block(0.1, 424)]]);
    expect(posted).toHaveLength(2);
  });

  test("takes the mean of the channels of a stereo input", () => {
    const { posted, registered } = load();
    const tap = new registered["levi-pcm-tap"]();
    for (let i = 0; i < 4; i += 1) tap.process([[block(0.5), block(-0.1)]]);
    const { pcm } = posted[0].message;
    expect(pcm[0]).toBeCloseTo(0.2, 6);
    expect(pcm[511]).toBeCloseTo(0.2, 6);
  });

  test("an input with no channels keeps the processor alive and posts nothing", () => {
    const { posted, registered } = load();
    const tap = new registered["levi-pcm-tap"]();
    expect(tap.process([])).toBe(true);
    expect(tap.process([[]])).toBe(true);
    expect(posted).toHaveLength(0);
  });

  test("makes no network request and loads nothing", () => {
    for (const forbidden of [
      "fetch(",
      "XMLHttpRequest",
      "WebSocket",
      "importScripts",
      "import(",
    ])
      expect(source).not.toContain(forbidden);
  });
});
