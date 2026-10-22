// Typed client of the marvel0's voice routes (`/api/levi/marvel0/speech`,
// `speech/transcribe`, `speech/match`, `speech/release`). It rides on the same
// request machinery as the other action routes (deploy-api.ts): same origin,
// the product frontend's proxy adds the person's token, so nothing here reads,
// stores or shows one; a deadline and an abort on every call; LEVI's error
// shape. A clip goes up as the raw body of one POST with Content-Type
// audio/wav; it is never kept (not here, not in the request, not in storage).
import { createCaller, type ApiResult, type Caller } from "./deploy-api";
import { MARVEL0_REQUEST_MS } from "./marvel0-logic";

/** How long each call may stay unanswered. A transcription waits for a cold
 * start of the speech worker (the model loads in about a second on a warm disk;
 * the server answers `speech_timeout` before this runs out). */
export interface SpeechDeadlines {
  status: number;
  transcribe: number;
  match: number;
  release: number;
}
export const SPEECH_DEADLINES: SpeechDeadlines = {
  status: MARVEL0_REQUEST_MS,
  transcribe: 75_000,
  match: 15_000,
  release: 15_000,
};

export interface SpeechApi {
  /** The state of the voice input and whether it may be used now. Passive: it
   * never starts the speech worker. */
  status(signal?: AbortSignal, modelId?: string): Promise<ApiResult>;
  /** One clip (16 kHz mono 16-bit WAV) for the model of the form (`modelId`
   * decides which instructions are allowed). */
  transcribe(
    clip: Blob,
    query: { requestId: string; modelId: string },
    signal?: AbortSignal,
  ): Promise<ApiResult>;
  /** Text only: matches typed or edited text against the allowed instructions. */
  match(
    body: { transcript: string; modelId: string },
    signal?: AbortSignal,
  ): Promise<ApiResult>;
  /** Unloads the speech model (frees the GPU memory). Idempotent. */
  release(signal?: AbortSignal): Promise<ApiResult>;
}

const WAV = "audio/wav";

export function createSpeechApi({
  call,
  fetchFn,
  deadlines = {},
}: {
  /** The request machinery; by default the one of the other marvel0 routes. */
  call?: Caller;
  /** Or only the fetch it runs on (tests). */
  fetchFn?: NonNullable<Parameters<typeof createCaller>[0]>["fetchFn"];
  deadlines?: Partial<SpeechDeadlines>;
} = {}): SpeechApi {
  const limit = { ...SPEECH_DEADLINES, ...deadlines };
  const send = call ?? createCaller({ fetchFn });
  return {
    status: (signal, modelId) =>
      send(
        modelId
          ? `speech?${new URLSearchParams({ model_id: modelId })}`
          : "speech",
        limit.status,
        undefined,
        signal,
      ),
    transcribe: (clip, { requestId, modelId }, signal) => {
      const query = new URLSearchParams({ request_id: requestId });
      if (modelId) query.set("model_id", modelId);
      // The server reads the Content-Type; a Blob of another type is relabelled
      // (the bytes are the page's own WAV either way).
      const raw = clip.type === WAV ? clip : clip.slice(0, clip.size, WAV);
      return send(
        `speech/transcribe?${query.toString()}`,
        limit.transcribe,
        { raw },
        signal,
      );
    },
    match: ({ transcript, modelId }, signal) =>
      send(
        "speech/match",
        limit.match,
        { transcript, model_id: modelId },
        signal,
      ),
    release: (signal) => send("speech/release", limit.release, {}, signal),
  };
}

/** The client the card uses. */
export const speechApi: SpeechApi = createSpeechApi();
