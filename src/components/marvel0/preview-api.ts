// Typed client of the camera preview's routes (`/api/levi/marvel0/preview...`,
// levi/marvel0/preview_api.py): the state, a start, a stop and the newest
// picture of a camera. Same origin: the proxy adds the person's token, nothing
// here reads one. The preview opens the cameras only when a person asks and
// only while no client can hold them; nothing here talks to a robot-side port.
import { createCaller, type ApiResult } from "./deploy-api";
import { MARVEL0_REQUEST_MS } from "./marvel0-logic";
import { fetchFrame, type FrameCamera, type FrameResult } from "./run-api";

/** The backend's reasons for a 404 of `preview/frame`. */
const PREVIEW_FRAME_REASONS: readonly string[] = ["preview_off", "no_frame"];

export interface PreviewDeadlines {
  read: number;
  /** A start spawns the worker and answers at once. */
  start: number;
  /** A stop waits until the worker has exited (up to 6 s). */
  stop: number;
}
export const PREVIEW_DEADLINES: PreviewDeadlines = {
  read: MARVEL0_REQUEST_MS,
  start: 15_000,
  stop: 15_000,
};

export interface PreviewApi {
  read(signal?: AbortSignal): Promise<ApiResult>;
  start(): Promise<ApiResult>;
  stop(): Promise<ApiResult>;
  /** The newest picture of one camera. Resolves (never rejects) unless
   * `signal` aborts it. */
  frame(camera: FrameCamera, signal?: AbortSignal): Promise<FrameResult>;
}

export function createPreviewApi({
  fetchFn,
  deadlines = {},
}: {
  fetchFn?: (input: string, init?: RequestInit) => Promise<Response>;
  deadlines?: Partial<PreviewDeadlines>;
} = {}): PreviewApi {
  const limit = { ...PREVIEW_DEADLINES, ...deadlines };
  const call = createCaller({ fetchFn });
  return {
    read: (signal) => call("preview", limit.read, undefined, signal),
    // (a bodiless write: the routes take none; the empty object makes it a POST)
    start: () => call("preview/start", limit.start, {}),
    stop: () => call("preview/stop", limit.stop, {}),
    frame: (camera, signal) =>
      fetchFrame(
        fetchFn,
        "preview/frame",
        PREVIEW_FRAME_REASONS,
        camera,
        signal,
      ),
  };
}

/** The client the page uses. */
export const previewApi: PreviewApi = createPreviewApi();
