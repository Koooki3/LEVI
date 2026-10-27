import { setupDom } from "@/components/ds/__tests__/dom";
import { afterEach, describe, expect, jest, test } from "bun:test";
import {
  VoiceCapture,
  attachPushToTalk,
  micConstraints,
  type CaptureOptions,
  type Clip,
  type DeviceInUse,
} from "../voice-capture";
import {
  installFakeAudio,
  type FakeAudio,
  type FakeAudioOptions,
} from "./speech-fixtures";

setupDom();

let audio: FakeAudio | null = null;
afterEach(() => {
  audio?.restore();
  audio = null;
  jest.useRealTimers();
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    value: "visible",
  });
});

/** Lets promise callbacks run (fake timers do not touch microtasks). */
async function settle() {
  for (let i = 0; i < 20; i += 1) await Promise.resolve();
}

function harness(
  audioOptions: FakeAudioOptions = {},
  options: Partial<CaptureOptions> = {},
) {
  jest.useFakeTimers();
  const fake = installFakeAudio(audioOptions);
  audio = fake;
  const clips: Clip[] = [];
  const errors: string[] = [];
  const mic: boolean[] = [];
  const levels: number[] = [];
  const states: string[] = [];
  const opened: DeviceInUse[] = [];
  const cap = new VoiceCapture(
    {
      onClip: (clip) => clips.push(clip),
      onError: (error) => errors.push(error.code),
      onMic: (open) => mic.push(open),
      onLevel: (level) => levels.push(level.rmsDb),
      onState: (next) => states.push(next),
      onDevice: (device) => opened.push(device),
    },
    options,
  );
  return { audio: fake, cap, clips, errors, mic, levels, states, opened };
}

/** Parses a clip's WAV: the header fields and the samples. */
async function parse(clip: Clip) {
  const bytes = new Uint8Array(await clip.blob.arrayBuffer());
  const view = new DataView(bytes.buffer);
  const text = (at: number) =>
    String.fromCharCode(...bytes.subarray(at, at + 4));
  return {
    bytes,
    riff: text(0),
    wave: text(8),
    fmt: text(12),
    data: text(36),
    fmtSize: view.getUint32(16, true),
    tag: view.getUint16(20, true),
    channels: view.getUint16(22, true),
    rate: view.getUint32(24, true),
    bits: view.getUint16(34, true),
    dataSize: view.getUint32(40, true),
    riffSize: view.getUint32(4, true),
    samples: new Int16Array(bytes.buffer.slice(44)),
  };
}

describe("VoiceCapture", () => {
  test("opens nothing when it is made, nor while it idles", () => {
    const h = harness();
    jest.advanceTimersByTime(120_000);
    expect(h.audio.getUserMediaCalls).toBe(0);
    expect(h.audio.contexts).toHaveLength(0);
    expect(h.cap.state).toBe("idle");
  });

  test("a press opens the microphone and delivers a 16 kHz mono 16-bit WAV, tail included", async () => {
    const h = harness();
    expect(await h.cap.start()).toBe(true);
    expect(h.audio.getUserMediaCalls).toBe(1);
    expect(h.audio.requestedRates).toEqual([16000]);
    expect(h.audio.workletUrls).toEqual(["/voice-worklet.js"]);
    h.audio.seconds(1.2);
    h.cap.stop();
    h.audio.seconds(0.2); // the device goes on during the 200 ms tail
    jest.advanceTimersByTime(200);
    expect(h.errors).toEqual([]);
    expect(h.clips).toHaveLength(1);
    const clip = h.clips[0];
    expect(clip.path).toBe("audioworklet");
    expect(clip.truncated).toBe(false);
    expect(clip.ms).toBeGreaterThan(1380);
    expect(clip.ms).toBeLessThan(1450);
    const wav = await parse(clip);
    expect([wav.riff, wav.wave, wav.fmt, wav.data]).toEqual([
      "RIFF",
      "WAVE",
      "fmt ",
      "data",
    ]);
    expect([wav.fmtSize, wav.tag, wav.channels, wav.rate, wav.bits]).toEqual([
      16, 1, 1, 16000, 16,
    ]);
    expect(wav.bytes.length).toBe(44 + wav.samples.length * 2);
    expect(wav.dataSize).toBe(wav.samples.length * 2);
    expect(wav.riffSize).toBe(wav.bytes.length - 8);
    expect(clip.bytes).toBe(wav.bytes.length);
    expect(clip.blob.type).toBe("audio/wav");
  });

  test("the tail is wall-clock: with no more audio the clip ends 200 ms after the release", async () => {
    const h = harness();
    await h.cap.start();
    h.audio.seconds(1);
    h.cap.stop();
    jest.advanceTimersByTime(199);
    expect(h.clips).toHaveLength(0);
    jest.advanceTimersByTime(1);
    expect(h.clips).toHaveLength(1);
  });

  test("keeps the microphone for 8 s after a clip, then stops the tracks and closes the context", async () => {
    const h = harness();
    await h.cap.start();
    h.audio.seconds(1);
    h.cap.stop();
    jest.advanceTimersByTime(200);
    expect(h.mic).toEqual([true]);
    jest.advanceTimersByTime(7_999);
    expect(h.audio.streams[0].track.readyState).toBe("live");
    jest.advanceTimersByTime(1);
    expect(h.audio.streams[0].track.readyState).toBe("ended");
    expect(h.audio.contexts[0].state).toBe("closed");
    expect(h.mic).toEqual([true, false]);
  });

  test("a second press while warm reuses the microphone; after the release it asks again", async () => {
    const h = harness();
    for (let round = 0; round < 2; round += 1) {
      await h.cap.start();
      h.audio.seconds(0.6);
      h.cap.stop();
      jest.advanceTimersByTime(200);
      jest.advanceTimersByTime(3_000);
    }
    expect(h.audio.getUserMediaCalls).toBe(1);
    expect(h.clips).toHaveLength(2);
    jest.advanceTimersByTime(10_000);
    expect(h.audio.streams[0].track.readyState).toBe("ended");
    await h.cap.start();
    expect(h.audio.getUserMediaCalls).toBe(2);
  });

  test("abort drops the recording and releases the microphone at once", async () => {
    const h = harness();
    await h.cap.start();
    h.audio.seconds(1);
    h.cap.abort();
    jest.advanceTimersByTime(20_000);
    expect(h.clips).toHaveLength(0);
    expect(h.errors).toEqual([]);
    expect(h.audio.streams[0].track.readyState).toBe("ended");
    expect(h.audio.contexts[0].state).toBe("closed");
    expect(h.cap.state).toBe("idle");
  });

  test("dispose during a recording releases everything, sends no clip and ignores a late block", async () => {
    const h = harness();
    await h.cap.start();
    h.audio.seconds(0.5);
    const late = h.audio.contexts[0].workletNode!.port.onmessage!;
    h.cap.dispose();
    expect(h.audio.streams[0].track.readyState).toBe("ended");
    expect(h.audio.contexts[0].state).toBe("closed");
    expect(() =>
      late({ data: { pcm: new Float32Array(512), peak: 0.1, rms: 0.1 } }),
    ).not.toThrow();
    jest.advanceTimersByTime(30_000);
    expect(h.clips).toHaveLength(0);
    expect(await h.cap.start()).toBe(false);
    expect(h.audio.getUserMediaCalls).toBe(1);
  });

  test("dispose while the microphone is still opening stops it as soon as it opens", async () => {
    const h = harness({ gate: true });
    const started = h.cap.start();
    await settle();
    expect(h.cap.state).toBe("requesting");
    h.cap.dispose();
    h.audio.open();
    expect(await started).toBe(false);
    expect(h.audio.streams[0].track.readyState).toBe("ended");
    expect(h.audio.contexts).toHaveLength(0);
  });

  test("a release before the microphone is ready is 'not_ready', not an empty clip", async () => {
    const h = harness({ gate: true });
    const started = h.cap.start();
    await settle();
    h.cap.stop();
    h.audio.open();
    expect(await started).toBe(false);
    expect(h.errors).toEqual(["not_ready"]);
    expect(h.clips).toHaveLength(0);
    expect(h.cap.state).toBe("idle");
    // the microphone stays warm, so the next press records at once
    expect(h.audio.streams[0].track.readyState).toBe("live");
    expect(await h.cap.start()).toBe(true);
    expect(h.audio.getUserMediaCalls).toBe(1);
  });

  test("stops at 10 s, counted in audio frames, and trims to exactly 10 s", async () => {
    const h = harness();
    await h.cap.start();
    h.audio.seconds(11);
    expect(h.clips).toHaveLength(1);
    const clip = h.clips[0];
    expect(clip.truncated).toBe(true);
    expect(clip.ms).toBe(10_000);
    expect(clip.bytes).toBe(44 + 160_000 * 2);
    expect(clip.bytes).toBeLessThanOrEqual(400_000);
    expect(h.cap.state).toBe("idle");
  });

  test("the cap also holds when the device runs at 48 kHz and is resampled", async () => {
    const h = harness({ refuseRate: true, deviceRate: 48000 });
    await h.cap.start();
    h.audio.seconds(11);
    const clip = h.clips[0];
    expect(clip.deviceRate).toBe(48000);
    expect(clip.truncated).toBe(true);
    expect(clip.bytes).toBe(44 + 160_000 * 2);
  });

  test("a wall-clock backstop ends a recording whose audio stopped arriving", async () => {
    const h = harness();
    await h.cap.start();
    h.audio.seconds(0.5);
    jest.advanceTimersByTime(11_499);
    expect(h.clips).toHaveLength(0);
    jest.advanceTimersByTime(1);
    expect(h.clips).toHaveLength(1);
    expect(h.clips[0].truncated).toBe(true);
    expect(h.clips[0].ms).toBeCloseTo(500, -2);
  });

  test("a clip under 300 ms is not sent, and neither is one with no sound at all", async () => {
    const h = harness();
    await h.cap.start();
    h.audio.seconds(0.1);
    h.cap.stop();
    jest.advanceTimersByTime(200);
    expect(h.errors).toEqual(["too_short"]);
    await h.cap.start();
    h.audio.seconds(1, 0);
    h.cap.stop();
    jest.advanceTimersByTime(200);
    expect(h.errors).toEqual(["too_short", "silent"]);
    expect(h.clips).toHaveLength(0);
  });

  test("meters the level from the same samples, only while recording", async () => {
    const h = harness();
    await h.cap.start();
    h.audio.block(512, 0.5);
    expect(h.levels).toHaveLength(1);
    expect(h.levels[0]).toBeCloseTo(-9, 0);
    h.cap.abort();
    expect(h.levels).toHaveLength(1);
  });

  test("a device that goes away mid-recording ends it with 'disconnected' and no clip", async () => {
    const h = harness();
    await h.cap.start();
    h.audio.seconds(0.5);
    h.audio.streams[0].track.end();
    expect(h.errors).toEqual(["disconnected"]);
    expect(h.clips).toHaveLength(0);
    expect(h.cap.state).toBe("idle");
    expect(h.audio.contexts[0].state).toBe("closed");
  });

  describe("what the browser says", () => {
    const cases: Array<[string, string]> = [
      ["NotAllowedError", "denied"],
      ["NotFoundError", "no_device"],
      ["NotReadableError", "busy"],
      ["AbortError", "busy"],
    ];
    for (const [name, code] of cases)
      test(`${name} is '${code}' and leaves nothing open`, async () => {
        const h = harness({ error: name });
        expect(await h.cap.start()).toBe(false);
        expect(h.errors).toEqual([code]);
        expect(h.cap.state).toBe("idle");
        expect(h.audio.contexts).toHaveLength(0);
      });

    test("a page that is not a secure context never asks for the microphone", async () => {
      const h = harness({ secure: false });
      expect(await h.cap.start()).toBe(false);
      expect(h.errors).toEqual(["insecure_context"]);
      expect(h.audio.getUserMediaCalls).toBe(0);
    });

    test("a worklet that does not load is not reported as a busy microphone: the ScriptProcessor takes over", async () => {
      const h = harness({ failWorklet: true });
      expect(await h.cap.start()).toBe(true);
      h.audio.seconds(1);
      h.cap.stop();
      jest.advanceTimersByTime(200);
      expect(h.errors).toEqual([]);
      expect(h.clips).toHaveLength(1);
      expect(h.clips[0].path).toBe("scriptprocessor");
    });

    test("no worklet and no ScriptProcessor is an audio problem, not a busy microphone", async () => {
      const h = harness({ noWorklet: true, noScriptProcessor: true });
      expect(await h.cap.start()).toBe(false);
      expect(h.errors).toEqual(["audio_graph"]);
      expect(h.audio.streams[0].track.readyState).toBe("ended");
    });
  });

  describe("the sample rate", () => {
    test("a browser that refuses the 16 kHz context gets the device rate and a resample", async () => {
      const h = harness({ refuseRate: true, deviceRate: 48000 });
      await h.cap.start();
      h.audio.seconds(1);
      h.cap.stop();
      jest.advanceTimersByTime(200);
      expect(h.audio.contexts[0].sampleRate).toBe(48000);
      const clip = h.clips[0];
      expect(clip.deviceRate).toBe(48000);
      const wav = await parse(clip);
      expect(wav.rate).toBe(16000);
      // 94 blocks of 512 frames at 48 kHz, a third of them at 16 kHz
      expect(wav.samples.length).toBe(Math.floor((94 * 512) / 3));
    });

    test("a microphone that cannot feed a context of another rate rebuilds it at the device rate", async () => {
      const h = harness({ refuseSource: true, deviceRate: 48000 });
      await h.cap.start();
      h.audio.seconds(1);
      h.cap.stop();
      jest.advanceTimersByTime(200);
      expect(h.audio.requestedRates).toEqual([16000, undefined]);
      expect(h.audio.contexts[0].state).toBe("closed");
      expect(h.clips[0].deviceRate).toBe(48000);
      expect(h.errors).toEqual([]);
    });
  });

  test("a stereo input is down-mixed to mono, not cut to its left channel", async () => {
    const h = harness({ noWorklet: true });
    await h.cap.start();
    const left = new Float32Array(512).fill(0.5);
    const right = new Float32Array(512).fill(-0.1);
    for (let i = 0; i < 40; i += 1) h.audio.channels([left, right]);
    h.cap.stop();
    jest.advanceTimersByTime(200);
    const wav = await parse(h.clips[0]);
    const peak = Math.max(...Array.from(wav.samples, Math.abs));
    expect(peak / 32767).toBeCloseTo(0.2, 1); // the mean of 0.5 and -0.1
  });
});

describe("the chosen input", () => {
  const UGREEN = { deviceId: "ugreen-1", label: "UGREEN USB MIC-CM727" };
  const CAMERA = { deviceId: "insta-1", label: "Insta360 Ace Pro 2" };
  /** Records 0.6 s and lets the clip come out; the microphone stays warm. */
  async function clipOnce(h: ReturnType<typeof harness>) {
    expect(await h.cap.start()).toBe(true);
    h.audio.seconds(0.6);
    h.cap.stop();
    jest.advanceTimersByTime(200);
  }

  test("without a choice nothing names a device; the input that opened is reported", async () => {
    const h = harness({ devices: [CAMERA, UGREEN] });
    await clipOnce(h);
    expect(h.audio.constraints).toEqual([{ audio: { channelCount: 1 } }]);
    expect(h.opened).toEqual([
      { id: CAMERA.deviceId, label: CAMERA.label, fallback: false },
    ]);
    expect(h.clips).toHaveLength(1);
  });

  test("a chosen input is asked for exactly, so the browser cannot substitute another", async () => {
    const h = harness({ devices: [CAMERA, UGREEN] });
    h.cap.setDevice(UGREEN.deviceId);
    await clipOnce(h);
    expect(h.audio.constraints).toEqual([
      { audio: { channelCount: 1, deviceId: { exact: UGREEN.deviceId } } },
    ]);
    expect(micConstraints(UGREEN.deviceId)).toEqual(h.audio.constraints[0]);
    expect(h.opened).toEqual([
      { id: UGREEN.deviceId, label: UGREEN.label, fallback: false },
    ]);
    expect(h.errors).toEqual([]);
  });

  test("a chosen input that is gone opens the default for this press and says so", async () => {
    const h = harness({ devices: [CAMERA] });
    h.cap.setDevice(UGREEN.deviceId);
    await clipOnce(h);
    expect(h.audio.constraints).toEqual([
      { audio: { channelCount: 1, deviceId: { exact: UGREEN.deviceId } } },
      { audio: { channelCount: 1 } },
    ]);
    expect(h.opened).toEqual([
      { id: CAMERA.deviceId, label: CAMERA.label, fallback: true },
    ]);
    expect(h.errors).toEqual([]);
    expect(h.clips).toHaveLength(1);
  });

  test("a refusal other than a missing input is not papered over with the default", async () => {
    const h = harness({ devices: [UGREEN], error: "NotAllowedError" });
    h.cap.setDevice(UGREEN.deviceId);
    expect(await h.cap.start()).toBe(false);
    expect(h.audio.getUserMediaCalls).toBe(1);
    expect(h.errors).toEqual(["denied"]);
    expect(h.opened).toEqual([]);
  });

  test("changing the input releases a warm microphone at once, and the next press opens the new one", async () => {
    const h = harness({ devices: [CAMERA, UGREEN] });
    await clipOnce(h);
    expect(h.mic).toEqual([true]);
    h.cap.setDevice(UGREEN.deviceId);
    expect(h.mic).toEqual([true, false]);
    expect(h.audio.streams[0].track.readyState).toBe("ended");
    expect(h.audio.contexts[0].state).toBe("closed");
    await clipOnce(h);
    expect(h.audio.getUserMediaCalls).toBe(2);
    expect(h.opened.map((device) => device.id)).toEqual([
      CAMERA.deviceId,
      UGREEN.deviceId,
    ]);
    // Setting the same input again changes nothing.
    h.cap.setDevice(UGREEN.deviceId);
    expect(h.mic).toEqual([true, false, true]);
  });

  test("a change during a recording keeps that recording on its input, then releases it at once", async () => {
    const h = harness({ devices: [CAMERA, UGREEN] });
    expect(await h.cap.start()).toBe(true);
    h.audio.seconds(0.5);
    h.cap.setDevice(UGREEN.deviceId);
    expect(h.audio.streams[0].track.readyState).toBe("live");
    h.audio.seconds(0.2);
    h.cap.stop();
    jest.advanceTimersByTime(200);
    expect(h.clips).toHaveLength(1);
    expect(h.clips[0].ms).toBeGreaterThan(650);
    // Not kept warm for 8 s: it is no longer the chosen input.
    expect(h.audio.streams[0].track.readyState).toBe("ended");
    expect(h.mic).toEqual([true, false]);
    await h.cap.start();
    expect(h.audio.getUserMediaCalls).toBe(2);
    expect(h.audio.streams[0].track.readyState).toBe("ended");
    expect(h.opened[1]).toEqual({
      id: UGREEN.deviceId,
      label: UGREEN.label,
      fallback: false,
    });
  });
});

describe("attachPushToTalk", () => {
  function press(h: ReturnType<typeof harness>) {
    const button = document.createElement("button");
    document.body.appendChild(button);
    const detach = attachPushToTalk(button, h.cap);
    const clicks: number[] = [];
    button.addEventListener("click", () => clicks.push(1));
    const captured: number[] = [];
    button.setPointerCapture = (id: number) => void captured.push(id);
    const pointer = (type: string, init: PointerEventInit = {}) => {
      const event = new PointerEvent(type, {
        pointerId: 7,
        pointerType: "mouse",
        button: 0,
        bubbles: true,
        cancelable: true,
        ...init,
      });
      button.dispatchEvent(event);
      return event;
    };
    const key = (
      type: "keydown" | "keyup",
      k: string,
      init: KeyboardEventInit = {},
    ) => {
      const event = new KeyboardEvent(type, {
        key: k,
        bubbles: true,
        cancelable: true,
        ...init,
      });
      button.dispatchEvent(event);
      return event;
    };
    return { button, detach, clicks, captured, pointer, key };
  }

  test("holding the pointer records while held and ends with the release; pointer capture is taken", async () => {
    const h = harness();
    const b = press(h);
    b.pointer("pointerdown");
    await settle();
    expect(h.audio.getUserMediaCalls).toBe(1);
    expect(b.captured).toEqual([7]);
    h.audio.seconds(0.4);
    jest.advanceTimersByTime(400);
    b.pointer("pointerup");
    h.audio.seconds(0.2);
    jest.advanceTimersByTime(200);
    expect(h.clips).toHaveLength(1);
    b.detach();
  });

  test("a tap under 250 ms latches the recording until the next tap", async () => {
    const h = harness();
    const b = press(h);
    b.pointer("pointerdown");
    await settle();
    jest.advanceTimersByTime(80);
    b.pointer("pointerup");
    h.audio.seconds(1.5);
    jest.advanceTimersByTime(1_500);
    expect(h.cap.state).toBe("recording");
    expect(h.clips).toHaveLength(0);
    b.pointer("pointerdown", { pointerId: 8 });
    b.pointer("pointerup", { pointerId: 8 });
    h.audio.seconds(0.2);
    jest.advanceTimersByTime(200);
    expect(h.clips).toHaveLength(1);
    expect(h.cap.state).toBe("idle");
    b.detach();
  });

  test("a tap that ends while the microphone is still opening only enables it ('not_ready')", async () => {
    const h = harness({ gate: true });
    const b = press(h);
    b.pointer("pointerdown");
    await settle();
    jest.advanceTimersByTime(80);
    b.pointer("pointerup");
    h.audio.open();
    await settle();
    expect(h.errors).toEqual(["not_ready"]);
    expect(h.cap.state).toBe("idle");
    b.detach();
  });

  test("a right mouse button does nothing", async () => {
    const h = harness();
    const b = press(h);
    b.pointer("pointerdown", { button: 2 });
    await settle();
    expect(h.audio.getUserMediaCalls).toBe(0);
    b.detach();
  });

  test("Space and Enter on the focused button record while held; both keydown and keyup are cancelled, repeats ignored, no click", async () => {
    const h = harness();
    const b = press(h);
    for (const k of [" ", "Enter"]) {
      const down = b.key("keydown", k);
      await settle();
      expect(down.defaultPrevented).toBe(true);
      const repeat = b.key("keydown", k, { repeat: true });
      expect(repeat.defaultPrevented).toBe(true);
      h.audio.seconds(0.6);
      jest.advanceTimersByTime(600);
      const up = b.key("keyup", k);
      expect(up.defaultPrevented).toBe(true);
      jest.advanceTimersByTime(200);
    }
    expect(h.audio.getUserMediaCalls).toBe(1);
    expect(h.clips).toHaveLength(2);
    expect(b.clicks).toEqual([]);
    b.detach();
  });

  test("an auto-repeated keydown never starts a recording (the key was pressed before focus came here)", async () => {
    const h = harness();
    const b = press(h);
    const repeat = b.key("keydown", " ", { repeat: true });
    await settle();
    expect(repeat.defaultPrevented).toBe(true);
    expect(h.audio.getUserMediaCalls).toBe(0);
    expect(h.cap.state).toBe("idle");
    b.detach();
  });

  test("other keys and key combinations are left alone", async () => {
    const h = harness();
    const b = press(h);
    expect(b.key("keydown", "a").defaultPrevented).toBe(false);
    expect(b.key("keydown", "Enter", { ctrlKey: true }).defaultPrevented).toBe(
      false,
    );
    await settle();
    expect(h.audio.getUserMediaCalls).toBe(0);
    b.detach();
  });

  test("a click that comes from neither pointer nor key (a screen reader) toggles the latched recording", async () => {
    const h = harness();
    const b = press(h);
    b.button.click();
    await settle();
    expect(h.cap.state).toBe("recording");
    h.audio.seconds(1);
    b.button.click();
    jest.advanceTimersByTime(200);
    expect(h.clips).toHaveLength(1);
    b.detach();
  });

  test("losing the window's focus while recording drops the clip and releases the microphone", async () => {
    const h = harness();
    const b = press(h);
    b.pointer("pointerdown");
    await settle();
    h.audio.seconds(1);
    window.dispatchEvent(new Event("blur"));
    expect(h.audio.streams[0].track.readyState).toBe("ended");
    b.pointer("pointerup");
    jest.advanceTimersByTime(1_000);
    expect(h.clips).toHaveLength(0);
    b.detach();
  });

  test("hiding the tab does the same, even for a latched recording", async () => {
    const h = harness();
    const b = press(h);
    b.pointer("pointerdown");
    await settle();
    jest.advanceTimersByTime(50);
    b.pointer("pointerup"); // latched
    h.audio.seconds(1);
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value: "hidden",
    });
    document.dispatchEvent(new Event("visibilitychange"));
    expect(h.audio.streams[0].track.readyState).toBe("ended");
    expect(h.cap.state).toBe("idle");
    jest.advanceTimersByTime(30_000);
    expect(h.clips).toHaveLength(0);
    b.detach();
  });

  test("a menu on long press is cancelled, and a detached button does nothing", async () => {
    const h = harness();
    const b = press(h);
    const menu = new Event("contextmenu", { cancelable: true, bubbles: true });
    b.button.dispatchEvent(menu);
    expect(menu.defaultPrevented).toBe(true);
    b.detach();
    b.pointer("pointerdown");
    await settle();
    expect(h.audio.getUserMediaCalls).toBe(0);
  });
});

describe("asynchronous microphone cancellation", () => {
  test("a cancelled permission request cannot close a restarted recording when it rejects late", async () => {
    const h = harness();
    let rejectPermission!: (reason: Error) => void;
    const permission = new Promise<MediaStream>((_resolve, reject) => {
      rejectPermission = reject;
    });
    const getUserMedia = navigator.mediaDevices.getUserMedia;
    navigator.mediaDevices.getUserMedia = () => permission;
    const oldStart = h.cap.start();
    await settle();
    h.cap.abort();
    navigator.mediaDevices.getUserMedia = getUserMedia;
    expect(await h.cap.start()).toBe(true);
    const states = [...h.states];
    const mic = [...h.mic];
    rejectPermission(
      Object.assign(new Error("late denial"), { name: "NotAllowedError" }),
    );
    expect(await oldStart).toBe(false);
    expect(h.cap.state).toBe("recording");
    expect(h.cap.micOpen).toBe(true);
    expect(h.audio.streams[0].track.readyState).toBe("live");
    expect(h.audio.contexts[0].state).toBe("running");
    expect(h.states).toEqual(states);
    expect(h.mic).toEqual(mic);
    expect(h.errors).toEqual([]);
    h.audio.seconds(0.6);
    h.cap.stop();
    jest.advanceTimersByTime(200);
    expect(h.clips).toHaveLength(1);
  });

  test("a disposed permission request emits no late failure or state event", async () => {
    const h = harness();
    let rejectPermission!: (reason: Error) => void;
    const permission = new Promise<MediaStream>((_resolve, reject) => {
      rejectPermission = reject;
    });
    navigator.mediaDevices.getUserMedia = () => permission;
    const started = h.cap.start();
    await settle();
    h.cap.dispose();
    const states = [...h.states];
    const mic = [...h.mic];
    rejectPermission(
      Object.assign(new Error("late denial"), { name: "NotAllowedError" }),
    );
    expect(await started).toBe(false);
    expect(h.states).toEqual(states);
    expect(h.mic).toEqual(mic);
    expect(h.errors).toEqual([]);
    expect(h.audio.contexts).toEqual([]);
    expect(h.audio.streams).toEqual([]);
  });

  for (const action of ["abort", "dispose"] as const) {
    for (const fails of [false, true]) {
      test(`${action} during a pending worklet closes the late graph (${fails ? "reject" : "resolve"})`, async () => {
        let resolve!: () => void;
        let reject!: (error: Error) => void;
        const workletReady = new Promise<void>((yes, no) => {
          resolve = yes;
          reject = no;
        });
        const h = harness({ workletReady });
        const started = h.cap.start();
        await settle();
        expect(h.audio.workletUrls).toHaveLength(1);
        h.cap[action]();
        // No resolution/rejection has arrived yet: the hardware is already off.
        expect(h.audio.streams[0].track.readyState).toBe("ended");
        expect(h.audio.contexts.every((ctx) => ctx.state === "closed")).toBe(
          true,
        );
        if (fails) reject(new Error("worklet unavailable"));
        else resolve();
        expect(await started).toBe(false);
        expect(h.audio.streams[0].track.readyState).toBe("ended");
        expect(h.audio.contexts.every((ctx) => ctx.state === "closed")).toBe(
          true,
        );
        expect(h.audio.contexts[0].path).not.toBe("scriptprocessor");
        expect(h.mic).not.toContain(true);
        expect(h.clips).toEqual([]);
      });
    }
  }

  test("moving focus within the page while Space is held releases the mic without upload", async () => {
    const h = harness();
    const button = document.createElement("button");
    const input = document.createElement("input");
    document.body.append(button, input);
    const detach = attachPushToTalk(button, h.cap);
    try {
      button.focus();
      button.dispatchEvent(
        new KeyboardEvent("keydown", {
          key: " ",
          bubbles: true,
          cancelable: true,
        }),
      );
      await settle();
      h.audio.seconds(1);
      input.focus();
      input.dispatchEvent(
        new KeyboardEvent("keyup", { key: " ", bubbles: true }),
      );
      jest.advanceTimersByTime(1000);
      expect(h.cap.state).toBe("idle");
      expect(h.audio.streams[0].track.readyState).toBe("ended");
      expect(h.clips).toEqual([]);
    } finally {
      detach();
      button.remove();
      input.remove();
    }
  });
});
