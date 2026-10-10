// Typed client of the live service control routes (`/api/levi/live/service*`).
// Same origin: the product frontend's proxy adds the person's token, so
// nothing here reads, stores or shows one. A write carries a `request_id`.
import type { ServiceOperation, ServiceStatus } from "./live-service-logic";

export type ApiResult<T> =
  | { ok: true; status: number; data: T }
  | {
      ok: false;
      status: number;
      code: string;
      message: string;
      /** The operation that is running (423 busy). */
      operationId?: string;
    };

/** The core answers `{detail: {code, message, …}}`; the AERI routes answer
 * `{error: {code, message}}`; a proxy or framework error is plain text. */
export function errorOf(
  status: number,
  body: unknown,
): Extract<ApiResult<never>, { ok: false }> {
  const record = (body && typeof body === "object" ? body : {}) as Record<
    string,
    unknown
  >;
  const inner = (record.detail ?? record.error ?? record) as unknown;
  const fields = (inner && typeof inner === "object" ? inner : {}) as Record<
    string,
    unknown
  >;
  const text = (value: unknown) => (typeof value === "string" ? value : "");
  return {
    ok: false,
    status,
    code: text(fields.code) || `http_${status}`,
    message:
      text(fields.message) ||
      (typeof inner === "string" ? inner : "") ||
      `HTTP ${status}`,
    operationId: text(fields.operation_id) || undefined,
  };
}

async function call<T>(
  path: string,
  body?: unknown,
  signal?: AbortSignal,
): Promise<ApiResult<T>> {
  try {
    const response = await fetch(`/api/levi/live/${path}`, {
      cache: "no-store",
      signal,
      ...(body === undefined
        ? {}
        : {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          }),
    });
    const data = await response.json().catch(() => null);
    if (!response.ok) return errorOf(response.status, data);
    return { ok: true, status: response.status, data: data as T };
  } catch (error) {
    if (signal?.aborted) throw error;
    return {
      ok: false,
      status: 0,
      code: "network",
      message: error instanceof Error ? error.message : String(error),
    };
  }
}

export interface ServiceApi {
  status(signal?: AbortSignal): Promise<ApiResult<ServiceStatus>>;
  start(body: unknown): Promise<ApiResult<ServiceOperation>>;
  stop(body: unknown): Promise<ApiResult<ServiceOperation>>;
  operation(
    id: string,
    signal?: AbortSignal,
  ): Promise<ApiResult<ServiceOperation>>;
}

export const serviceApi: ServiceApi = {
  status: (signal) => call<ServiceStatus>("service", undefined, signal),
  start: (body) => call<ServiceOperation>("service/start", body),
  stop: (body) => call<ServiceOperation>("service/stop", body),
  operation: (id, signal) =>
    call<ServiceOperation>(
      `service/operations/${encodeURIComponent(id)}`,
      undefined,
      signal,
    ),
};
