// Helpers of the voice card's tests: the answers of the voice routes as the
// backend writes them (design record 5.3 and 5.4), a SpeechApi made of canned
// answers that records its calls, a stand-in for the microphone capture, and a
// fake browser audio stack (getUserMedia, AudioContext, AudioWorklet).
import type { ApiResult } from "../deploy-api";
import type { SpeechApi } from "../speech-api";
import type {
  CaptureEvents,
  CaptureLike,
  CaptureState,
  Clip,
} from "../voice-capture";
import { encodeWav16 } from "../voice-capture";

type Rec = Record<string, unknown>;

export const KNOWN = [
  "pick screws of same size into the empty slot of screw box",
  "stack the plates of same color together",
  "pick the eggplant in the blue plate",
];
export const RESTRICTED = KNOWN.slice(0, 2);

export const ok = (data: unknown, status = 200): ApiResult => ({
  ok: true,
  status,
  data,
});

// -- the answers

export function statusAnswer(over: Rec = {}): Rec {
  return {
    schema: "levi.marvel0.speech.status.v1",
    allowed_revision: "test-revision",
    state: "off",
    reason: null,
    installed: { worker: true, model: true },
    model: "qwen3-asr-1.7b-hf",
    device: "cuda",
    limits: { max_seconds: 10, max_bytes: 400000 },
    idle_seconds: 120,
    gate: { allowed: true, reasons: [] },
    ...over,
  };
}

export function candidate(prompt: string, over: Rec = {}): Rec {
  return { prompt, score: 95, fixes: [], differences: [], ...over };
}

/** A match of tier A: one sentence, preselected. */
export function matchA(prompt = KNOWN[2], over: Rec = {}): Rec {
  return {
    tier: "A",
    allowed_revision: "test-revision",
    flag: null,
    family: "all",
    best: candidate(prompt),
    alternatives: [],
    allowed: KNOWN,
    ...over,
  };
}

/** A match of tier B: the best and two alternatives. */
export function matchB(over: Rec = {}): Rec {
  return {
    tier: "B",
    allowed_revision: "test-revision",
    flag: null,
    family: "all",
    best: candidate(KNOWN[1], {
      score: 74,
      differences: [
        { code: "color_mismatch", said: "red", expected: "same color" },
      ],
    }),
    alternatives: [
      candidate(KNOWN[0], { score: 66 }),
      candidate(KNOWN[2], { score: 61 }),
    ],
    allowed: KNOWN,
    ...over,
  };
}

/** A match of tier C: nothing is close. */
export function matchC(over: Rec = {}): Rec {
  return {
    tier: "C",
    allowed_revision: "test-revision",
    flag: null,
    family: "all",
    best: null,
    alternatives: [],
    allowed: KNOWN,
    ...over,
  };
}

/** A match of tier V: a flag, no candidates. */
export function matchV(flag: string | null = "stop", over: Rec = {}): Rec {
  return {
    tier: "V",
    allowed_revision: "test-revision",
    flag,
    family: "all",
    best: null,
    alternatives: [],
    allowed: KNOWN,
    ...over,
  };
}

/** The answer of POST speech/transcribe for a request id. */
export function heardAnswer(
  requestId: string,
  match: Rec | null = matchA(),
  over: Rec = {},
): Rec {
  return {
    schema: "levi.marvel0.speech.v1",
    ok: true,
    request_id: requestId,
    heard: true,
    reason: null,
    transcript: "pick the eggplant in the blue plate",
    language: "English",
    match,
    engine: { model: "qwen3-asr-1.7b-hf", device: "cuda", loaded_now: false },
    timing_ms: { load: 0, infer: 104 },
    ...over,
  };
}

export function matchedAnswer(match: Rec): Rec {
  return { schema: "levi.marvel0.speech.match.v1", match };
}

// -- a SpeechApi of canned answers

export interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
}
export function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

export interface TranscribeCall {
  clip: Blob;
  requestId: string;
  modelId: string;
  signal?: AbortSignal;
}
export interface MatchCall {
  transcript: string;
  modelId: string;
  signal?: AbortSignal;
}
export interface SpeechCalls {
  status: number;
  transcribe: TranscribeCall[];
  match: MatchCall[];
  release: number;
}

type Later<T> = T | Promise<T>;
export interface Script {
  status?: ApiResult | (() => Later<ApiResult>);
  transcribe?: ApiResult | ((call: TranscribeCall) => Later<ApiResult>);
  match?: ApiResult | ((call: MatchCall) => Later<ApiResult>);
  release?: ApiResult | (() => Later<ApiResult>);
}

/** Rejects like a real call does when its signal aborts. */
function abortable<T>(
  signal: AbortSignal | undefined,
  work: Later<T>,
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    if (signal?.aborted) return reject(new Error("aborted"));
    signal?.addEventListener("abort", () => reject(new Error("aborted")), {
      once: true,
    });
    Promise.resolve(work).then(resolve, reject);
  });
}

export function fakeSpeechApi(script: Script = {}): {
  api: SpeechApi;
  calls: SpeechCalls;
} {
  const calls: SpeechCalls = {
    status: 0,
    transcribe: [],
    match: [],
    release: 0,
  };
  const run = <A extends unknown[]>(
    answer: ApiResult | ((...args: A) => Later<ApiResult>) | undefined,
    fallback: string,
    ...args: A
  ): Later<ApiResult> => {
    if (!answer) throw new Error(`unexpected ${fallback}`);
    return typeof answer === "function" ? answer(...args) : answer;
  };
  const api: SpeechApi = {
    status: (signal) => {
      calls.status += 1;
      return abortable(
        signal,
        run(script.status ?? ok(statusAnswer()), "status"),
      );
    },
    transcribe: (clip, query, signal) => {
      const call = { clip, ...query, signal };
      calls.transcribe.push(call);
      return abortable(signal, run(script.transcribe, "transcribe", call));
    },
    match: (body, signal) => {
      const call = { ...body, signal };
      calls.match.push(call);
      return abortable(signal, run(script.match, "match", call));
    },
    release: () => {
      calls.release += 1;
      return Promise.resolve(
        run(script.release ?? ok({ state: "off", released: true }), "release"),
      );
    },
  };
  return { api, calls };
}

// -- a stand-in for the capture

export function fakeClip(over: Partial<Clip> = {}): Clip {
  const samples = new Int16Array(16000);
  return {
    blob: encodeWav16(samples),
    ms: 1000,
    bytes: 44 + samples.length * 2,
    peakDb: -12,
    rmsDb: -20,
    truncated: false,
    path: "audioworklet",
    deviceRate: 48000,
    ...over,
  };
}

/** Behaves like VoiceCapture towards its events: a press goes idle to
 * recording, a finished clip goes through encoding to idle and then arrives. */
export class FakeCapture implements CaptureLike {
  state: CaptureState = "idle";
  starts = 0;
  stops = 0;
  aborts = 0;
  disposed = false;
  /** Every `setDevice` call, in order. */
  devices: Array<string | null> = [];
  private readonly listeners = new Set<(next: CaptureState) => void>();
  constructor(readonly events: CaptureEvents) {}

  private set(next: CaptureState) {
    const prev = this.state;
    if (prev === next) return;
    this.state = next;
    this.events.onState?.(next, prev);
    for (const listener of [...this.listeners]) listener(next);
  }
  addStateListener(listener: (next: CaptureState) => void) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }
  async start() {
    this.starts += 1;
    if (this.state !== "idle") return false;
    this.set("recording");
    return true;
  }
  stop() {
    this.stops += 1;
  }
  cancel() {
    if (this.state === "recording") this.set("idle");
  }
  abort() {
    this.aborts += 1;
    if (this.state !== "idle") this.set("idle");
  }
  dispose() {
    this.disposed = true;
    this.listeners.clear();
  }
  setDevice(deviceId: string | null) {
    this.devices.push(deviceId);
  }
  /** The input the capture would use next (the last `setDevice`). */
  get device(): string | null | undefined {
    return this.devices[this.devices.length - 1];
  }
  // -- drivers
  /** The microphone opened on this input. */
  opened(device: { id: string | null; label: string; fallback?: boolean }) {
    this.events.onMic?.(true);
    this.events.onDevice?.({ fallback: false, ...device });
  }
  /** The recording ended and produced a clip. */
  deliver(over: Partial<Clip> = {}) {
    this.set("encoding");
    this.set("idle");
    this.events.onClip?.(fakeClip(over));
  }
  /** The recording ended without a clip. */
  fail(code: Parameters<NonNullable<CaptureEvents["onError"]>>[0]["code"]) {
    if (this.state === "recording") {
      this.set("encoding");
      this.set("idle");
    }
    this.events.onError?.({ code });
  }
}

/** The captures made so far, newest last; pass `make` as `createCapture`. */
export function captureFactory() {
  const made: FakeCapture[] = [];
  return {
    made,
    make: (events: CaptureEvents): CaptureLike => {
      const capture = new FakeCapture(events);
      made.push(capture);
      return capture;
    },
    get current(): FakeCapture {
      const last = made[made.length - 1];
      if (!last) throw new Error("no capture was made");
      return last;
    },
  };
}

// -- a fake browser audio stack

export interface FakeAudioOptions {
  /** The rate the device delivers (and a context made without a rate runs at). */
  deviceRate?: number;
  /** `new AudioContext({sampleRate})` throws, as Firefox does for a microphone. */
  refuseRate?: boolean;
  /** A context made at a rate other than the device's cannot take the stream. */
  refuseSource?: boolean;
  /** addModule rejects (a CSP block, a 404). */
  failWorklet?: boolean;
  workletReady?: Promise<void>;
  /** The context has no audioWorklet at all. */
  noWorklet?: boolean;
  /** No ScriptProcessor either. */
  noScriptProcessor?: boolean;
  /** getUserMedia waits for `release()` of its gate. */
  gate?: boolean;
  /** getUserMedia rejects with an error of this name. */
  error?: string;
  secure?: boolean;
  /** What enumerateDevices lists (kind defaults to audioinput). With a list,
   * getUserMedia refuses an `exact` deviceId that is not in it, as a browser
   * does (OverconstrainedError), and opens the first input for the default. */
  devices?: FakeDevice[];
  /** The error an `exact` id that is not in `devices` raises (browsers use
   * OverconstrainedError; some report NotFoundError). */
  goneAs?: "OverconstrainedError" | "NotFoundError";
}

export interface FakeDevice {
  deviceId: string;
  label: string;
  kind?: string;
}

export class FakeTrack {
  readyState: "live" | "ended" = "live";
  label = "";
  deviceId = "";
  private readonly handlers: Array<() => void> = [];
  constructor(private readonly stream: FakeStream) {}
  getSettings() {
    return this.deviceId ? { deviceId: this.deviceId } : {};
  }
  stop() {
    this.readyState = "ended";
    this.stream.active = false;
  }
  addEventListener(type: string, handler: () => void) {
    if (type === "ended") this.handlers.push(handler);
  }
  /** The device went away. */
  end() {
    this.readyState = "ended";
    this.stream.active = false;
    for (const handler of this.handlers) handler();
  }
}
export class FakeStream {
  active = true;
  readonly track = new FakeTrack(this);
  getAudioTracks() {
    return [this.track];
  }
  getTracks() {
    return [this.track];
  }
}

export class FakeNode {
  onaudioprocess: ((event: unknown) => void) | null = null;
  port = { onmessage: null as ((event: { data: unknown }) => void) | null };
  connected = true;
  connect() {}
  disconnect() {
    this.connected = false;
  }
}

export interface FakeAudio {
  getUserMediaCalls: number;
  /** The constraints of every getUserMedia call, in order. */
  constraints: MediaStreamConstraints[];
  enumerateCalls: number;
  /** Replaces the device list and fires `devicechange` (unless `fire` is
   * false: the browser revealing names after permission fires nothing). */
  setDevices(devices: FakeDevice[], fire?: boolean): void;
  contexts: FakeContext[];
  streams: FakeStream[];
  /** The rates the contexts were asked for (undefined: the device's own). */
  requestedRates: Array<number | undefined>;
  workletUrls: string[];
  /** Lets a gated getUserMedia (or the next one) answer. */
  open(): void;
  /** Delivers `frames` samples of a sine to the newest context's node. */
  block(frames?: number, amplitude?: number, hz?: number): void;
  /** Delivers `seconds` of sound in blocks of 512 frames. */
  seconds(seconds: number, amplitude?: number, hz?: number): void;
  /** Delivers one block made of the given channels (stereo input). */
  channels(channels: Float32Array[]): void;
  restore(): void;
}

export class FakeContext {
  state: "running" | "closed" = "running";
  readonly sampleRate: number;
  readonly destination = {};
  readonly node = new FakeNode();
  workletNode: FakeNode | null = null;
  path: "audioworklet" | "scriptprocessor" | null = null;
  phase = 0;
  readonly audioWorklet?: { addModule(url: string): Promise<void> };
  constructor(
    rate: number,
    private readonly options: FakeAudioOptions,
    private readonly audio: FakeAudio,
  ) {
    this.sampleRate = rate;
    if (!options.noWorklet)
      this.audioWorklet = {
        addModule: async (url: string) => {
          audio.workletUrls.push(url);
          await options.workletReady;
          if (options.failWorklet)
            throw Object.assign(
              new Error("Unable to load a worklet's module."),
              {
                name: "AbortError",
              },
            );
        },
      };
  }
  createMediaStreamSource() {
    if (
      this.options.refuseSource &&
      this.sampleRate !== (this.options.deviceRate ?? 48000)
    )
      throw Object.assign(new Error("different sample-rate"), {
        name: "NotSupportedError",
      });
    return new FakeNode();
  }
  createGain() {
    return Object.assign(new FakeNode(), { gain: { value: 1 } });
  }
  createScriptProcessor() {
    this.path = "scriptprocessor";
    return this.node;
  }
  async resume() {}
  async close() {
    this.state = "closed";
  }
}

type G = Record<string, unknown>;

/** Installs fakes for navigator.mediaDevices, AudioContext, AudioWorkletNode and
 * isSecureContext. `restore()` puts back what was there. */
export function installFakeAudio(options: FakeAudioOptions = {}): FakeAudio {
  const g = globalThis as unknown as G;
  const navigatorObject = g.navigator as unknown as G;
  const saved = {
    mediaDevices: Object.getOwnPropertyDescriptor(
      navigatorObject,
      "mediaDevices",
    ),
    secure: Object.getOwnPropertyDescriptor(g, "isSecureContext"),
    audioContext: g.AudioContext,
    worklet: g.AudioWorkletNode,
  };
  const deviceRate = options.deviceRate ?? 48000;
  let gate = options.gate ? deferred<void>() : null;
  let devices = options.devices;
  const changeListeners = new Set<() => void>();
  const inputs = () =>
    (devices ?? []).filter(
      (device) => (device.kind ?? "audioinput") === "audioinput",
    );
  const audio: FakeAudio = {
    getUserMediaCalls: 0,
    constraints: [],
    enumerateCalls: 0,
    setDevices(next, fire = true) {
      devices = next;
      if (fire) for (const listener of [...changeListeners]) listener();
    },
    contexts: [],
    streams: [],
    requestedRates: [],
    workletUrls: [],
    open() {
      gate?.resolve();
    },
    block(frames = 512, amplitude = 0.3, hz = 440) {
      const context = audio.contexts[audio.contexts.length - 1];
      if (!context) throw new Error("no audio context yet");
      const pcm = new Float32Array(frames);
      for (let i = 0; i < frames; i += 1) {
        pcm[i] = amplitude * Math.sin(context.phase);
        context.phase += (2 * Math.PI * hz) / context.sampleRate;
      }
      deliver(context, [pcm]);
    },
    seconds(seconds, amplitude = 0.3, hz = 440) {
      const context = audio.contexts[audio.contexts.length - 1];
      if (!context) throw new Error("no audio context yet");
      const blocks = Math.round((seconds * context.sampleRate) / 512);
      for (let i = 0; i < blocks; i += 1) audio.block(512, amplitude, hz);
    },
    channels(channels) {
      const context = audio.contexts[audio.contexts.length - 1];
      if (!context) throw new Error("no audio context yet");
      deliver(context, channels);
    },
    restore() {
      if (saved.mediaDevices)
        Object.defineProperty(
          navigatorObject,
          "mediaDevices",
          saved.mediaDevices,
        );
      else delete navigatorObject.mediaDevices;
      if (saved.secure)
        Object.defineProperty(g, "isSecureContext", saved.secure);
      else delete g.isSecureContext;
      g.AudioContext = saved.audioContext;
      g.AudioWorkletNode = saved.worklet;
    },
  };

  /** Hands a block to whatever the capture built. */
  function deliver(context: FakeContext, channels: Float32Array[]) {
    if (context.workletNode?.port.onmessage) {
      const mono = new Float32Array(channels[0].length);
      for (const channel of channels)
        for (let i = 0; i < mono.length; i += 1)
          mono[i] += channel[i] / channels.length;
      let peak = 0;
      let sum = 0;
      for (const sample of mono) {
        peak = Math.max(peak, Math.abs(sample));
        sum += sample * sample;
      }
      context.workletNode.port.onmessage({
        data: { pcm: mono, peak, rms: Math.sqrt(sum / mono.length) },
      });
    } else if (context.node.onaudioprocess) {
      context.node.onaudioprocess({
        inputBuffer: {
          numberOfChannels: channels.length,
          getChannelData: (c: number) => channels[c],
        },
      });
    } else throw new Error("nothing is listening for audio");
  }

  Object.defineProperty(navigatorObject, "mediaDevices", {
    configurable: true,
    value: {
      getUserMedia: async (constraints: MediaStreamConstraints = {}) => {
        audio.getUserMediaCalls += 1;
        audio.constraints.push(constraints);
        if (gate) {
          await gate.promise;
          gate = null;
        }
        if (options.error)
          throw Object.assign(new Error(options.error), {
            name: options.error,
          });
        const asked =
          typeof constraints.audio === "object"
            ? (constraints.audio.deviceId as { exact?: string } | undefined)
                ?.exact
            : undefined;
        let device: FakeDevice | undefined;
        if (devices) {
          device = asked
            ? inputs().find((input) => input.deviceId === asked)
            : inputs()[0];
          if (!device)
            throw Object.assign(new Error("no such device"), {
              name: asked
                ? (options.goneAs ?? "OverconstrainedError")
                : "NotFoundError",
            });
        }
        const stream = new FakeStream();
        stream.track.label = device?.label ?? "";
        stream.track.deviceId = device?.deviceId ?? "";
        audio.streams.push(stream);
        return stream;
      },
      enumerateDevices: async () => {
        audio.enumerateCalls += 1;
        return (devices ?? []).map((device) => ({
          deviceId: device.deviceId,
          label: device.label,
          kind: device.kind ?? "audioinput",
          groupId: "",
        }));
      },
      addEventListener: (type: string, listener: () => void) => {
        if (type === "devicechange") changeListeners.add(listener);
      },
      removeEventListener: (type: string, listener: () => void) => {
        if (type === "devicechange") changeListeners.delete(listener);
      },
    },
  });
  Object.defineProperty(g, "isSecureContext", {
    configurable: true,
    value: options.secure ?? true,
  });
  class AudioContextFake extends FakeContext {
    constructor(init?: { sampleRate?: number }) {
      if (init?.sampleRate !== undefined && options.refuseRate)
        throw Object.assign(new Error("sample rate not supported"), {
          name: "NotSupportedError",
        });
      super(init?.sampleRate ?? deviceRate, options, audio);
      audio.requestedRates.push(init?.sampleRate);
      audio.contexts.push(this);
    }
  }
  g.AudioContext = AudioContextFake;
  class WorkletNodeFake extends FakeNode {
    constructor(context: FakeContext) {
      super();
      context.workletNode = this;
      context.path = "audioworklet";
    }
  }
  g.AudioWorkletNode = WorkletNodeFake;
  if (options.noScriptProcessor)
    // A context class without createScriptProcessor.
    (AudioContextFake.prototype as unknown as G).createScriptProcessor =
      undefined;
  return audio;
}

/** The generated block of a match (owner decision 2026-10-30). */
export function generated(over: Rec = {}): Rec {
  return {
    status: "ok",
    prompt: "pick eggplant into blue bowl",
    frames: ["pick {A} into {B}"],
    episodes: 117,
    slots: [
      { said: "茄子", english: "eggplant", source: "lexicon" },
      { said: "碗", english: "bowl", source: "lexicon" },
    ],
    untranslated: [],
    dataset_original: "put eggplant into blue bowl",
    ...over,
  };
}
