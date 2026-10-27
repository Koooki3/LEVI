// Microphone capture for the voice card of the marvel0: a 16 kHz mono 16-bit
// WAV of what the person says while holding a button, and nothing else.
//
//   * The microphone is opened only inside a press (`start`, called from the
//     button's pointer or key handler), never when the page loads. It is kept
//     for `keepWarmMs` after a clip so the next press starts at once, then its
//     tracks are stopped and the audio context closed. `abort` (window blur,
//     tab hidden) and `dispose` (unmount) release it immediately.
//   * Samples come from an AudioWorklet loaded from the same-origin file
//     /voice-worklet.js (public/voice-worklet.js), with a ScriptProcessor
//     fallback when the worklet cannot load. The level meter reads the same
//     samples. The context asks for 16 kHz; a browser that refuses (Firefox
//     cannot feed a microphone into a context of another rate) gets a windowed
//     sinc resampler: Chromium's own offline resampler has no anti-alias filter
//     (an 11 kHz tone landed at 5 kHz, -6 dB).
//   * A clip is at most `maxMs` long, counted in audio frames (with a
//     wall-clock backstop for a device that stops delivering), and runs on for
//     `tailMs` after the release so the end of the last word is not cut.
//   * `setDevice` picks the input (mic-choice.ts); null is the system default.
//     A chosen input that cannot be found is replaced by the default for that
//     opening, and `onDevice` says which input was opened and whether that
//     happened. Changing the input releases a warm microphone at once.
//
// Nothing here touches the network, storage or the DOM beyond the button the
// press handlers are attached to.

export const WORKLET_URL = "/voice-worklet.js";
export const WORKLET_NAME = "levi-pcm-tap";
/** The rate of the clip the server accepts (levi.marvel0.speech: 16 kHz mono). */
export const OUT_RATE = 16_000;
export const MAX_RECORD_MS = 10_000;

const DB_FLOOR = -100;
/** A clip whose loudest sample is below this carries no sound at all (a muted
 * or disconnected microphone); it is not sent. */
const SILENT_PEAK_DB = -50;

export const toDb = (amplitude: number): number =>
  amplitude > 1e-5 ? Math.max(DB_FLOOR, 20 * Math.log10(amplitude)) : DB_FLOOR;

/** 0..1 for a bar: -60 dBFS and below is empty, 0 dBFS is full. */
export const dbToFraction = (db: number): number =>
  Math.min(1, Math.max(0, (db + 60) / 60));

export type CaptureState = "idle" | "requesting" | "recording" | "encoding";

/** Why no clip came out. The page words each of them in both languages. */
export type CaptureErrorCode =
  | "insecure_context"
  | "unsupported"
  | "denied"
  | "no_device"
  | "busy"
  | "not_ready"
  | "too_short"
  | "silent"
  | "audio_graph"
  | "disconnected"
  | "encode"
  | "error";

export interface CaptureError {
  code: CaptureErrorCode;
}
export interface Level {
  rmsDb: number;
  peakDb: number;
}
export interface Clip {
  /** The WAV: `audio/wav`, exactly 44 bytes of header and the samples. */
  blob: Blob;
  ms: number;
  bytes: number;
  peakDb: number;
  rmsDb: number;
  /** The 10 s limit ended it. */
  truncated: boolean;
  path: "audioworklet" | "scriptprocessor";
  /** The rate the device delivered, when it was not 16 kHz the clip was resampled. */
  deviceRate: number;
}

export interface CaptureOptions {
  maxMs: number;
  /** Shorter clips are dropped as accidental. */
  minMs: number;
  tailMs: number;
  keepWarmMs: number;
  workletUrl: string;
  /** Rate asked of the audio context; null leaves it to the device. */
  contextRate: number | null;
  /** Use the ScriptProcessor path even where a worklet is available (tests). */
  forceScriptProcessor: boolean;
}
export const CAPTURE_DEFAULTS: CaptureOptions = {
  maxMs: MAX_RECORD_MS,
  minMs: 300,
  tailMs: 200,
  keepWarmMs: 8000,
  workletUrl: WORKLET_URL,
  contextRate: OUT_RATE,
  forceScriptProcessor: false,
};

/** The input a press opened. */
export interface DeviceInUse {
  /** The browser's id of it, when it says. */
  id: string | null;
  /** Its name ("" when the browser gives none). */
  label: string;
  /** A chosen input could not be found and the system default was opened. */
  fallback: boolean;
}

export interface CaptureEvents {
  onState?(next: CaptureState, prev: CaptureState): void;
  onLevel?(level: Level): void;
  onClip?(clip: Clip): void;
  onError?(error: CaptureError): void;
  /** The microphone was opened (true) or released (false). */
  onMic?(open: boolean): void;
  /** Which input was opened (after `onMic(true)`). */
  onDevice?(device: DeviceInUse): void;
}

/** The part of the capture the card uses; tests replace it. */
export interface CaptureLike {
  readonly state: CaptureState;
  start(): Promise<boolean>;
  stop(): void;
  cancel(): void;
  abort(): void;
  dispose(): void;
  addStateListener(listener: (next: CaptureState) => void): () => void;
  /** The input the next opening uses (null: the system default). */
  setDevice?(deviceId: string | null): void;
}

// -- what the browser offers

interface MicEnv {
  mediaDevices: MediaDevices | undefined;
  secure: boolean;
  AudioContextClass: typeof AudioContext | undefined;
}
// Read at the moment of use, so a test that installs fakes is seen.
function micEnv(): MicEnv {
  const g = globalThis as unknown as {
    navigator?: Navigator;
    isSecureContext?: boolean;
    AudioContext?: typeof AudioContext;
    webkitAudioContext?: typeof AudioContext;
  };
  return {
    mediaDevices: g.navigator?.mediaDevices,
    secure: g.isSecureContext === true,
    AudioContextClass: g.AudioContext ?? g.webkitAudioContext,
  };
}

/** Whether this page can capture at all, without opening anything. A page that
 * is not a secure context (anything but https or loopback http) has no
 * `navigator.mediaDevices`. */
export function micSupport(): "ok" | "insecure_context" | "unsupported" {
  const env = micEnv();
  if (!env.secure || !env.mediaDevices) return "insecure_context";
  if (
    typeof env.mediaDevices.getUserMedia !== "function" ||
    !env.AudioContextClass
  )
    return "unsupported";
  return "ok";
}

/** A failure of `getUserMedia` or of the audio graph, as a code. A worklet that
 * does not load is not one of them: the ScriptProcessor takes over. */
export function micErrorCode(error: unknown): CaptureErrorCode {
  const name =
    typeof error === "object" && error !== null && "name" in error
      ? String((error as { name: unknown }).name)
      : "";
  switch (name) {
    case "NotAllowedError":
    case "SecurityError":
      return "denied";
    case "NotFoundError":
    case "OverconstrainedError":
      return "no_device";
    case "NotReadableError":
    case "AbortError":
      return "busy";
    case "TypeError":
      return "unsupported";
    default:
      return "error";
  }
}

// -- samples to WAV

/** The mean of the channels, sample by sample. */
export function downmix(channels: Float32Array[]): Float32Array {
  if (channels.length === 1) return new Float32Array(channels[0]);
  const out = new Float32Array(channels[0]?.length ?? 0);
  for (const channel of channels)
    for (let i = 0; i < out.length; i += 1) out[i] += channel[i];
  for (let i = 0; i < out.length; i += 1) out[i] /= channels.length;
  return out;
}

const kernelCache = new Map<string, { table: Float32Array; taps: number }>();

/** A windowed-sinc low-pass and resample (Blackman window, 16 zero crossings, a
 * 1024-phase kernel table). The cutoff sits at 95 % of the lower Nyquist
 * frequency, so a tone above it is removed instead of folded back. */
export function resampleSinc(
  x: Float32Array,
  srcRate: number,
  dstRate: number,
  zeros = 16,
  phases = 1024,
): Float32Array {
  if (srcRate === dstRate) return x;
  const ratio = srcRate / dstRate;
  const fc = 0.5 * Math.min(1, dstRate / srcRate) * 0.95; // cycles per input sample
  const half = zeros / (2 * fc); // kernel half width in input samples
  const L = Math.ceil(half);
  const taps = 2 * L + 2;
  const cacheKey = `${srcRate}:${dstRate}:${zeros}:${phases}`;
  let kernel = kernelCache.get(cacheKey);
  if (!kernel) {
    const table = new Float32Array((phases + 1) * taps);
    const twoFc = 2 * fc;
    for (let p = 0; p <= phases; p += 1) {
      for (let j = 0; j < taps; j += 1) {
        const u = j - L - p / phases;
        if (Math.abs(u) >= half) continue;
        const a = Math.PI * twoFc * u;
        const t = u / half;
        table[p * taps + j] =
          twoFc *
          (u === 0 ? 1 : Math.sin(a) / a) *
          (0.42 +
            0.5 * Math.cos(Math.PI * t) +
            0.08 * Math.cos(2 * Math.PI * t));
      }
    }
    kernel = { table, taps };
    kernelCache.set(cacheKey, kernel);
  }
  const { table } = kernel;
  const n = Math.floor(x.length / ratio);
  const y = new Float32Array(n);
  for (let o = 0; o < n; o += 1) {
    const centre = o * ratio;
    const ci = Math.floor(centre);
    const base = Math.round((centre - ci) * phases) * taps;
    const start = ci - L;
    let acc = 0;
    if (start >= 0 && start + taps <= x.length)
      for (let j = 0; j < taps; j += 1) acc += x[start + j] * table[base + j];
    else
      for (let j = 0; j < taps; j += 1) {
        const index = start + j;
        if (index >= 0 && index < x.length) acc += x[index] * table[base + j];
      }
    y[o] = acc;
  }
  return y;
}

/** The WAV the server accepts (levi.marvel0.speech, design record 5.5): a RIFF
 * file whose 44-byte header is `fmt ` (16 bytes: PCM, 1 channel, `rate`, 16
 * bits) followed at once by `data`, and nothing after the samples. */
export function encodeWav16(samples: Int16Array, rate = OUT_RATE): Blob {
  const bytes = samples.length * 2;
  const buffer = new ArrayBuffer(44 + bytes);
  const view = new DataView(buffer);
  const tag = (offset: number, text: string) => {
    for (let i = 0; i < text.length; i += 1)
      view.setUint8(offset + i, text.charCodeAt(i));
  };
  tag(0, "RIFF");
  view.setUint32(4, 36 + bytes, true);
  tag(8, "WAVE");
  tag(12, "fmt ");
  view.setUint32(16, 16, true); // size of the fmt chunk
  view.setUint16(20, 1, true); // PCM
  view.setUint16(22, 1, true); // mono
  view.setUint32(24, rate, true);
  view.setUint32(28, rate * 2, true); // bytes per second
  view.setUint16(32, 2, true); // bytes per frame
  view.setUint16(34, 16, true); // bits per sample
  tag(36, "data");
  view.setUint32(40, bytes, true);
  new Int16Array(buffer, 44).set(samples);
  return new Blob([buffer], { type: "audio/wav" });
}

function toInt16(x: Float32Array): Int16Array {
  const out = new Int16Array(x.length);
  for (let i = 0; i < x.length; i += 1) {
    const s = Math.max(-1, Math.min(1, x[i]));
    out[i] = Math.round(s < 0 ? s * 0x8000 : s * 0x7fff);
  }
  return out;
}

// -- the capture

interface Rig {
  stream: MediaStream;
  ctx: AudioContext;
  node: AudioNode;
  sink: GainNode;
  path: Clip["path"];
  /** The input that was asked for when it was opened. */
  wanted: string | null;
}

/** Thrown inside `open` when the capture was aborted or disposed meanwhile. */
class Superseded extends Error {}
class GraphError extends Error {}

/** What `getUserMedia` is asked for: `channelCount` is advisory (a device may
 * deliver stereo at 44.1 or 48 kHz whatever is asked) and the browser's
 * defaults for echo, noise and gain stay on. A chosen input is asked for
 * `exact`, so the browser never quietly substitutes another one. */
export function micConstraints(
  deviceId: string | null,
): MediaStreamConstraints {
  return {
    audio: deviceId
      ? { channelCount: 1, deviceId: { exact: deviceId } }
      : { channelCount: 1 },
  };
}

/** The chosen input is not there (unplugged, or its id changed). */
function inputGone(error: unknown): boolean {
  const name =
    typeof error === "object" && error !== null && "name" in error
      ? String((error as { name: unknown }).name)
      : "";
  return name === "OverconstrainedError" || name === "NotFoundError";
}

/** The input a stream came from, as far as the browser tells. */
function deviceOf(stream: MediaStream, fallback: boolean): DeviceInUse {
  const track = stream.getAudioTracks()[0] as MediaStreamTrack | undefined;
  let id: string | null = null;
  try {
    const settings = track?.getSettings?.();
    if (typeof settings?.deviceId === "string" && settings.deviceId)
      id = settings.deviceId;
  } catch {
    // a browser without getSettings
  }
  const label = typeof track?.label === "string" ? track.label : "";
  return { id, label, fallback };
}

export class VoiceCapture implements CaptureLike {
  state: CaptureState = "idle";
  private readonly o: CaptureOptions;
  private readonly events: CaptureEvents;
  private readonly listeners = new Set<(next: CaptureState) => void>();
  private rig: Rig | null = null;
  private openingCleanup: (() => void) | null = null;
  private chunks: Float32Array[] = [];
  private frames = 0;
  /** Bumped by abort and dispose: an `open` that sees a newer value gives up. */
  private gen = 0;
  private disposed = false;
  private wantStop = false;
  private cancelled = false;
  /** The input to open (null: the system default). */
  private deviceId: string | null = null;
  private warmTimer: ReturnType<typeof setTimeout> | undefined;
  private tailTimer: ReturnType<typeof setTimeout> | undefined;
  private limitTimer: ReturnType<typeof setTimeout> | undefined;

  constructor(
    events: CaptureEvents = {},
    options: Partial<CaptureOptions> = {},
  ) {
    this.events = events;
    this.o = { ...CAPTURE_DEFAULTS, ...options };
  }

  get micOpen(): boolean {
    return this.rig !== null;
  }

  addStateListener(listener: (next: CaptureState) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private setState(next: CaptureState) {
    const prev = this.state;
    if (prev === next) return;
    this.state = next;
    this.events.onState?.(next, prev);
    for (const listener of [...this.listeners]) listener(next);
  }

  private fail(code: CaptureErrorCode) {
    this.events.onError?.({ code });
  }

  /** The input the next opening uses. A warm microphone that is idle is the
   * old input and is released now; a recording (or an opening) in progress
   * goes on with the input it started on, and the next press opens the new one. */
  setDevice(deviceId: string | null): void {
    const next = deviceId || null;
    if (next === this.deviceId) return;
    this.deviceId = next;
    if (this.state === "idle") this.release();
  }

  /** Starts a recording (called from a press). Resolves true once it records.
   * The first press opens the microphone, which may ask the person; a release
   * before that is answered with `not_ready`, never with an empty clip. */
  async start(): Promise<boolean> {
    if (this.state !== "idle" || this.disposed) return false;
    clearTimeout(this.warmTimer);
    this.wantStop = false;
    this.cancelled = false;
    const support = micSupport();
    if (support !== "ok") {
      this.fail(support);
      return false;
    }
    if (
      !this.rig ||
      !this.rig.stream.active ||
      this.rig.wanted !== this.deviceId
    ) {
      this.release();
      this.setState("requesting");
      const gen = this.gen;
      try {
        await this.open(gen);
      } catch (error) {
        // A late permission failure belongs to the old attempt; it must not
        // release a newer graph or report a problem after disposal.
        if (gen !== this.gen || this.disposed || error instanceof Superseded)
          return false;
        this.release();
        this.setState("idle");
        this.fail(
          error instanceof GraphError ? "audio_graph" : micErrorCode(error),
        );
        return false;
      }
      if (gen !== this.gen) return false;
      if (this.cancelled) {
        this.setState("idle");
        this.armWarm();
        return false;
      }
      if (this.wantStop) {
        this.setState("idle");
        this.armWarm();
        this.fail("not_ready");
        return false;
      }
    }
    this.chunks = [];
    this.frames = 0;
    this.setState("recording");
    // Wall-clock backstop in case audio callbacks stop (device unplugged, tab
    // throttled): the frame count alone would never reach the limit.
    this.limitTimer = setTimeout(() => this.finish(true), this.o.maxMs + 1500);
    return true;
  }

  /** The press ended: record on for the tail, then deliver the clip. */
  stop(): void {
    if (this.state === "recording") {
      if (this.tailTimer === undefined)
        this.tailTimer = setTimeout(() => {
          this.tailTimer = undefined;
          this.finish(false);
        }, this.o.tailMs);
    } else if (this.state === "requesting") this.wantStop = true;
  }

  /** Drops what was recorded; the microphone stays warm for a moment. */
  cancel(): void {
    this.cancelled = true;
    if (this.state === "recording") {
      this.dropRecording();
      this.setState("idle");
      this.armWarm();
    }
  }

  /** Drops what was recorded and releases the microphone now. */
  abort(): void {
    this.gen += 1;
    const was = this.state;
    this.dropRecording();
    this.release();
    if (was !== "idle") this.setState("idle");
  }

  /** For good: the page is leaving. */
  dispose(): void {
    this.disposed = true;
    this.abort();
    this.listeners.clear();
  }

  private dropRecording() {
    clearTimeout(this.limitTimer);
    clearTimeout(this.tailTimer);
    this.tailTimer = undefined;
    this.chunks = [];
    this.frames = 0;
  }

  private armWarm() {
    clearTimeout(this.warmTimer);
    // The input was changed meanwhile: the old one is not kept warm.
    if (this.rig && this.rig.wanted !== this.deviceId) {
      this.release();
      return;
    }
    this.warmTimer = setTimeout(() => this.release(), this.o.keepWarmMs);
  }

  private release() {
    clearTimeout(this.warmTimer);
    const opening = this.openingCleanup;
    this.openingCleanup = null;
    opening?.();
    const rig = this.rig;
    this.rig = null;
    if (!rig) return;
    try {
      for (const track of rig.stream.getTracks()) track.stop();
    } catch {
      // already ended
    }
    discard(rig.ctx, rig.node, rig.sink);
    this.events.onMic?.(false);
  }

  /** The device went away while open. */
  private lost(rig: Rig) {
    if (this.rig !== rig) return;
    const wasRecording = this.state === "recording";
    this.dropRecording();
    this.release();
    if (this.state !== "idle") this.setState("idle");
    if (wasRecording) this.fail("disconnected");
  }

  private async open(gen: number): Promise<void> {
    const env = micEnv();
    const wanted = this.deviceId;
    let stream: MediaStream;
    let fallback = false;
    try {
      stream = await env.mediaDevices!.getUserMedia(micConstraints(wanted));
    } catch (error) {
      // The chosen input is gone: the system default, and the card says so.
      if (wanted === null || !inputGone(error)) throw error;
      if (gen !== this.gen) throw new Superseded();
      stream = await env.mediaDevices!.getUserMedia(micConstraints(null));
      fallback = true;
    }
    if (gen !== this.gen) {
      stopTracks(stream);
      throw new Superseded();
    }
    let ctx: AudioContext | null = null;
    let node: AudioNode | null = null;
    let sink: GainNode | null = null;
    const cleanup = () => {
      stopTracks(stream);
      if (ctx) discard(ctx, node, sink);
    };
    this.openingCleanup = cleanup;
    try {
      const made = this.makeContext(env.AudioContextClass!, stream);
      ctx = made.ctx;
      if (ctx.state === "suspended") await ctx.resume().catch(() => undefined);
      if (gen !== this.gen) throw new Superseded();
      sink = ctx.createGain();
      sink.gain.value = 0;
      sink.connect(ctx.destination);
      const rig = { stream, ctx, wanted } as Rig;
      const built = await this.makeNode(ctx, gen, (pcm, peak, rms) =>
        this.block(rig, pcm, peak, rms),
      );
      node = built.node;
      if (gen !== this.gen) throw new Superseded();
      made.source.connect(node);
      node.connect(sink);
      Object.assign(rig, { node, sink, path: built.path });
      stream
        .getAudioTracks()[0]
        ?.addEventListener("ended", () => this.lost(rig));
      this.rig = rig;
      this.events.onMic?.(true);
      this.events.onDevice?.(deviceOf(stream, fallback));
    } catch (error) {
      cleanup();
      throw error;
    } finally {
      if (this.openingCleanup === cleanup) this.openingCleanup = null;
    }
  }

  /** The audio context at the rate of the clip, or at the device's own when the
   * browser refuses (then `finish` resamples). */
  private makeContext(Ctor: typeof AudioContext, stream: MediaStream) {
    const rate = this.o.contextRate;
    const plain = { latencyHint: "interactive" } as const;
    const create = (wanted: number | null): AudioContext => {
      if (wanted === null) return new Ctor(plain);
      try {
        return new Ctor({ ...plain, sampleRate: wanted });
      } catch {
        return new Ctor(plain);
      }
    };
    let ctx = create(rate);
    try {
      return { ctx, source: ctx.createMediaStreamSource(stream) };
    } catch (error) {
      // Firefox: a microphone cannot feed a context of another rate.
      if (rate === null) {
        discard(ctx, null, null);
        throw error;
      }
      discard(ctx, null, null);
      ctx = create(null);
      return { ctx, source: ctx.createMediaStreamSource(stream) };
    }
  }

  /** The node that hands over blocks of samples: the worklet, else a
   * ScriptProcessor. A worklet that fails to load is not an error of the
   * microphone and is not reported as one. */
  private async makeNode(
    ctx: AudioContext,
    gen: number,
    onBlock: (pcm: Float32Array, peak: number, rms: number) => void,
  ): Promise<{ node: AudioNode; path: Clip["path"] }> {
    if (ctx.audioWorklet && !this.o.forceScriptProcessor) {
      try {
        await ctx.audioWorklet.addModule(this.o.workletUrl);
        if (gen !== this.gen) throw new Superseded();
        const node = new AudioWorkletNode(ctx, WORKLET_NAME, {
          numberOfInputs: 1,
          numberOfOutputs: 1,
          channelCount: 1,
          channelCountMode: "explicit",
        });
        node.port.onmessage = (event: MessageEvent) => {
          const d = event.data as
            | { pcm?: unknown; peak?: unknown; rms?: unknown }
            | undefined;
          if (
            d?.pcm instanceof Float32Array &&
            typeof d.peak === "number" &&
            typeof d.rms === "number"
          )
            onBlock(d.pcm, d.peak, d.rms);
        };
        return { node, path: "audioworklet" };
      } catch (error) {
        if (error instanceof Superseded) throw error;
        // fall through to the ScriptProcessor
      }
    }
    if (gen !== this.gen) throw new Superseded();
    if (typeof ctx.createScriptProcessor !== "function") throw new GraphError();
    const node = ctx.createScriptProcessor(1024, 1, 1);
    node.onaudioprocess = (event) => {
      const input = event.inputBuffer;
      const channels: Float32Array[] = [];
      for (let c = 0; c < input.numberOfChannels; c += 1)
        channels.push(input.getChannelData(c));
      const pcm = downmix(channels);
      let peak = 0;
      let sum = 0;
      for (let i = 0; i < pcm.length; i += 1) {
        const a = Math.abs(pcm[i]);
        if (a > peak) peak = a;
        sum += pcm[i] * pcm[i];
      }
      onBlock(pcm, peak, Math.sqrt(sum / Math.max(1, pcm.length)));
    };
    return { node, path: "scriptprocessor" };
  }

  private block(rig: Rig, pcm: Float32Array, peak: number, rms: number) {
    // A late message after the microphone was released.
    if (this.rig !== rig || this.state !== "recording") return;
    this.events.onLevel?.({ rmsDb: toDb(rms), peakDb: toDb(peak) });
    this.chunks.push(pcm);
    this.frames += pcm.length;
    if (this.frames >= this.maxFrames(rig)) this.finish(true);
  }

  private maxFrames(rig: Rig): number {
    return Math.floor((this.o.maxMs * rig.ctx.sampleRate) / 1000);
  }

  private finish(truncated: boolean) {
    const rig = this.rig;
    if (this.state !== "recording" || !rig) return;
    const chunks = this.chunks;
    this.dropRecording();
    this.setState("encoding");
    const rate = rig.ctx.sampleRate;
    const limit = this.maxFrames(rig);
    let clip: Clip | null = null;
    let code: CaptureErrorCode | null = null;
    try {
      const flat = new Float32Array(
        Math.min(
          limit,
          chunks.reduce((n, chunk) => n + chunk.length, 0),
        ),
      );
      let at = 0;
      for (const chunk of chunks) {
        if (at >= flat.length) break;
        flat.set(chunk.subarray(0, flat.length - at), at);
        at += chunk.length;
      }
      const ms = (flat.length * 1000) / rate;
      const out = resampleSinc(flat, rate, OUT_RATE);
      const samples = toInt16(out);
      let peak = 0;
      let sum = 0;
      for (let i = 0; i < out.length; i += 1) {
        const a = Math.min(1, Math.abs(out[i]));
        if (a > peak) peak = a;
        sum += a * a;
      }
      const peakDb = toDb(peak);
      if (ms < this.o.minMs) code = "too_short";
      else if (peakDb < SILENT_PEAK_DB) code = "silent";
      else
        clip = {
          blob: encodeWav16(samples),
          ms,
          bytes: 44 + samples.length * 2,
          peakDb,
          rmsDb: toDb(Math.sqrt(sum / Math.max(1, out.length))),
          truncated,
          path: rig.path,
          deviceRate: rate,
        };
    } catch {
      code = "encode";
    }
    this.setState("idle");
    this.armWarm();
    if (clip) this.events.onClip?.(clip);
    else if (code) this.fail(code);
  }
}

function stopTracks(stream: MediaStream) {
  try {
    for (const track of stream.getTracks()) track.stop();
  } catch {
    // already ended
  }
}

/** Disconnects what was built and closes the context; every step may already
 * be done. */
function discard(
  ctx: AudioContext,
  node: AudioNode | null,
  sink: GainNode | null,
) {
  try {
    if (node) {
      const worklet = node as AudioWorkletNode;
      if (worklet.port) worklet.port.onmessage = null;
      (node as ScriptProcessorNode).onaudioprocess = null;
      node.disconnect();
    }
    sink?.disconnect();
  } catch {
    // already disconnected
  }
  try {
    if (ctx.state !== "closed") void ctx.close().catch(() => undefined);
  } catch {
    // already closed
  }
}

// -- the button

export interface PushToTalkOptions {
  /** A press shorter than this keeps recording until the next press. */
  tapMs?: number;
}

/** Hold the button to talk. By pointer (with pointer capture, so a release
 * outside the button still ends the clip) or by Space or Enter while the button
 * has focus. A tap shorter than `tapMs` keeps the recording going until the next
 * tap, for people who cannot hold. Returns the function that removes the
 * handlers. Leaving the window or the tab ends a hold and releases the microphone.
 *
 * Space and Enter are cancelled on keydown AND keyup (a keydown that is not
 * cancelled clicks the button on Enter, a keyup does on Space) and repeats are
 * ignored. A click that comes from neither a pointer nor those keys (a screen
 * reader activating the button) toggles the same latched recording. */
export function attachPushToTalk(
  button: HTMLElement,
  cap: CaptureLike,
  { tapMs = 250 }: PushToTalkOptions = {},
): () => void {
  let downAt = 0;
  let pointerId: number | null = null;
  let keyDown = false;
  let latched = false;
  let pointerSeen = false;

  const off = cap.addStateListener((state) => {
    if (state === "idle") latched = false;
  });
  const press = (): boolean => {
    if (latched) {
      latched = false;
      cap.stop();
      return false;
    }
    if (cap.state !== "idle") return false;
    downAt = performance.now();
    void cap.start();
    return true;
  };
  // A tap latches only a recording that is already running: a press that ends
  // while the microphone is still being opened (the browser may be asking the
  // person) only enables it, and the person presses again.
  const release = () => {
    if (cap.state === "recording" && performance.now() - downAt < tapMs)
      latched = true;
    else cap.stop();
  };
  const reset = () => {
    pointerId = null;
    keyDown = false;
    latched = false;
    pointerSeen = false;
  };

  const onPointerDown = (event: PointerEvent) => {
    if (event.pointerType === "mouse" && event.button !== 0) return;
    event.preventDefault();
    pointerSeen = true;
    if (pointerId !== null) return;
    if (press()) {
      pointerId = event.pointerId;
      try {
        button.setPointerCapture(event.pointerId);
      } catch {
        // a pointer that is already gone
      }
    }
  };
  // The click of a pointer comes right after its pointerup, in the same task.
  const forgetPointer = () => setTimeout(() => (pointerSeen = false), 0);
  const onPointerUp = (event: PointerEvent) => {
    forgetPointer();
    if (event.pointerId !== pointerId) return;
    pointerId = null;
    release();
  };
  const onPointerCancel = (event: PointerEvent) => {
    forgetPointer();
    if (event.pointerId !== pointerId) return;
    pointerId = null;
    latched = false;
    cap.cancel();
  };
  const isKey = (event: KeyboardEvent) =>
    (event.key === " " || event.key === "Enter") &&
    !event.ctrlKey &&
    !event.metaKey &&
    !event.altKey;
  const onKeyDown = (event: KeyboardEvent) => {
    if (!isKey(event)) return;
    event.preventDefault();
    if (event.repeat || keyDown) return;
    if (press()) keyDown = true;
  };
  const onKeyUp = (event: KeyboardEvent) => {
    if (!isKey(event)) return;
    event.preventDefault();
    if (!keyDown) return;
    keyDown = false;
    release();
  };
  const onClick = (event: MouseEvent) => {
    // A pointer's click comes after its pointerdown, which already acted.
    if (pointerSeen || keyDown) {
      pointerSeen = false;
      return;
    }
    if (event.detail !== 0) return;
    if (latched) {
      latched = false;
      cap.stop();
    } else if (cap.state === "idle") {
      void cap.start().then((started) => {
        if (started) latched = true;
      });
    }
  };
  const onContextMenu = (event: Event) => event.preventDefault();
  // The window lost focus or the tab was hidden: whoever was speaking is not
  // looking at the page any more. Nothing is sent and the microphone is released.
  const leave = () => {
    reset();
    cap.abort();
  };
  const onVisibility = () => {
    if (document.visibilityState === "hidden") leave();
  };

  button.addEventListener("pointerdown", onPointerDown);
  button.addEventListener("pointerup", onPointerUp);
  button.addEventListener("pointercancel", onPointerCancel);
  button.addEventListener("keydown", onKeyDown);
  button.addEventListener("keyup", onKeyUp);
  button.addEventListener("click", onClick);
  button.addEventListener("contextmenu", onContextMenu);
  button.addEventListener("blur", leave);
  window.addEventListener("blur", leave);
  document.addEventListener("visibilitychange", onVisibility);
  return () => {
    off();
    button.removeEventListener("pointerdown", onPointerDown);
    button.removeEventListener("pointerup", onPointerUp);
    button.removeEventListener("pointercancel", onPointerCancel);
    button.removeEventListener("keydown", onKeyDown);
    button.removeEventListener("keyup", onKeyUp);
    button.removeEventListener("click", onClick);
    button.removeEventListener("contextmenu", onContextMenu);
    button.removeEventListener("blur", leave);
    window.removeEventListener("blur", leave);
    document.removeEventListener("visibilitychange", onVisibility);
  };
}
