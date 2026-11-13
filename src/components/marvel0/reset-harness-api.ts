// This client deliberately has no live autonomy methods or configurable host.
import { createCaller, type ApiResult } from "./deploy-api";
import type { ResetScenario } from "./reset-harness-doc";
import { MARVEL0_REQUEST_MS } from "./marvel0-logic";

export interface ResetHarnessStart {
  request_id: string;
  scenario_id: ResetScenario;
  seed: number;
}
export interface ResetHarnessApi {
  catalog(signal?: AbortSignal): Promise<ApiResult>;
  run(runId: string, signal?: AbortSignal): Promise<ApiResult>;
  start(body: ResetHarnessStart): Promise<ApiResult>;
  stop(runId: string, body: { request_id: string }): Promise<ApiResult>;
}

export function createResetHarnessApi({
  fetchFn,
  deadline = MARVEL0_REQUEST_MS,
}: {
  fetchFn?: (input: string, init?: RequestInit) => Promise<Response>;
  deadline?: number;
} = {}): ResetHarnessApi {
  const call = createCaller({ fetchFn });
  const path = "reset-harness";
  return {
    catalog: (signal) => call(path, deadline, undefined, signal),
    run: (id, signal) =>
      call(
        `${path}/runs/${encodeURIComponent(id)}`,
        deadline,
        undefined,
        signal,
      ),
    start: ({ request_id, scenario_id, seed }) =>
      call(`${path}/runs`, deadline, { request_id, scenario_id, seed }),
    stop: (id, { request_id }) =>
      call(`${path}/runs/${encodeURIComponent(id)}/stop`, deadline, {
        request_id,
      }),
  };
}
export const resetHarnessApi = createResetHarnessApi();
