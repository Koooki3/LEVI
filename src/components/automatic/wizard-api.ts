// Typed client of the automatic-evaluation pages (levi2/design/
// aeri-ui-api-contract.md). Every request goes to the same-origin
// `/api/levi/...` proxy, which attaches the person's token: nothing here reads,
// stores or shows a token. A write (P) carries a `request_id` made once per
// intent, so a repeated press is the same request to the server.
import type {
  Capabilities,
  CampaignPlan,
  CampaignPlanRequest,
  CampaignReport,
  CampaignSnapshot,
  CommandResult,
  JobCreate,
  LaunchPlan,
  PendingCardEpisode,
  PoliciesResponse,
  SetupStep,
} from "./wizard-types";

export type FieldError = { field?: string; message: string };

/** A failed call: the HTTP status, the contract's error code and the server's
 * sentence; a 422 also carries the field errors. */
export class ApiError extends Error {
  status: number;
  code: string;
  errors: FieldError[];
  constructor(
    status: number,
    code: string,
    message: string,
    errors: FieldError[] = [],
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.errors = errors;
  }
  /** Whether the contract's code is `name`, with or without the `E_` prefix
   * and in any case (the contract writes both `E_…` and `job_exists`). */
  is(name: string): boolean {
    return (
      this.code.toLowerCase().replace(/^e_/, "") ===
      name.toLowerCase().replace(/^e_/, "")
    );
  }
}

function fieldErrors(raw: unknown): FieldError[] {
  if (!Array.isArray(raw)) return [];
  const out: FieldError[] = [];
  for (const item of raw) {
    if (typeof item === "string") out.push({ message: item });
    else if (item && typeof item === "object") {
      const entry = item as Record<string, unknown>;
      const message = entry.message ?? entry.msg ?? entry.error;
      const field = entry.field ?? entry.path ?? entry.loc;
      if (typeof message === "string")
        out.push({
          field: Array.isArray(field)
            ? field.join(".")
            : typeof field === "string"
              ? field
              : undefined,
          message,
        });
    }
  }
  return out;
}

/** Reads an error body into an ApiError. The contract's body is
 * `{"detail": {"code", "message", "errors"?}}` (as the live service answers);
 * `{"error": {...}}` is read the same way, and a plain `{"detail": "text"}`
 * from a proxy or framework gives the text. */
export async function errorOf(response: Response): Promise<ApiError> {
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  const record = (body && typeof body === "object" ? body : {}) as Record<
    string,
    unknown
  >;
  const asRecord = (value: unknown): Record<string, unknown> =>
    value && typeof value === "object" && !Array.isArray(value)
      ? (value as Record<string, unknown>)
      : {};
  const inner =
    Object.keys(asRecord(record.detail)).length > 0
      ? asRecord(record.detail)
      : asRecord(record.error);
  const text =
    typeof record.detail === "string"
      ? record.detail
      : Array.isArray(record.detail)
        ? JSON.stringify(record.detail)
        : "";
  const code =
    typeof inner.code === "string" ? inner.code : `HTTP_${response.status}`;
  const message =
    typeof inner.message === "string" && inner.message
      ? inner.message
      : text || `HTTP ${response.status}`;
  const errors = fieldErrors(
    inner.errors ??
      inner.details ??
      record.errors ??
      (Array.isArray(record.detail) ? record.detail : undefined),
  );
  return new ApiError(response.status, code, message, errors);
}

export async function apiRequest<T>(
  method: "GET" | "POST",
  path: string,
  body?: unknown,
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api/levi/${path}`, {
      method,
      cache: "no-store",
      ...(body === undefined
        ? {}
        : {
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          }),
    });
  } catch (error) {
    throw new ApiError(
      0,
      "E_NETWORK",
      error instanceof Error ? error.message : String(error),
    );
  }
  if (!response.ok) throw await errorOf(response);
  return (await response.json()) as T;
}

/** A fresh idempotency key. */
export function newRequestId(): string {
  const c = globalThis.crypto;
  if (c && typeof c.randomUUID === "function") return c.randomUUID();
  return `req-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

const AUTO = "automatic";

export const getSetupGuide = () =>
  apiRequest<{ steps: SetupStep[] }>("GET", `${AUTO}/setup-guide`);
export const getCapabilities = () =>
  apiRequest<Capabilities>("GET", `${AUTO}/capabilities`);
export const getPolicies = () =>
  apiRequest<PoliciesResponse>("GET", `${AUTO}/policies`);

export const createJob = (job: JobCreate) =>
  apiRequest<{ id: string }>("POST", `${AUTO}/jobs`, job);

export const planLaunch = (jobId: string, executionMode: string) =>
  apiRequest<LaunchPlan>("POST", `${AUTO}/plan`, {
    job_id: jobId,
    execution_mode: executionMode,
  });

export const launchRun = (
  jobId: string,
  plan: Pick<LaunchPlan, "plan_sha256" | "launch_token">,
  requestId: string,
) =>
  apiRequest<{ run_id: string }>("POST", `${AUTO}/runs`, {
    job_id: jobId,
    plan_sha256: plan.plan_sha256,
    launch_token: plan.launch_token,
    confirm: "launch",
    request_id: requestId,
  });

export const planCampaign = (request: CampaignPlanRequest) =>
  apiRequest<CampaignPlan>("POST", `${AUTO}/campaigns/plan`, request);

export const startCampaign = (
  request: CampaignPlanRequest,
  planSha256: string,
  requestId: string,
) =>
  apiRequest<{ campaign_id: string }>("POST", `${AUTO}/campaigns`, {
    ...request,
    plan_sha256: planSha256,
    confirm: "start-campaign",
    request_id: requestId,
  });

export const listCampaigns = () =>
  apiRequest<{ campaigns: CampaignSnapshot[] }>("GET", `${AUTO}/campaigns`);
export const getCampaign = (id: string) =>
  apiRequest<CampaignSnapshot>(
    "GET",
    `${AUTO}/campaigns/${encodeURIComponent(id)}`,
  );

export const confirmCampaign = (
  id: string,
  commandId: string,
  kind: "switch_policy" | "env" | "segment_done",
  challenge: string,
  options?: Record<string, boolean>,
) =>
  apiRequest<CommandResult>(
    "POST",
    `${AUTO}/campaigns/${encodeURIComponent(id)}/confirm`,
    {
      command_id: commandId,
      kind,
      challenge,
      ...(options && Object.keys(options).length ? { options } : {}),
    },
  );

/** Start a controller for a campaign whose controller is gone (202). */
export const attachCampaign = (id: string, requestId: string) =>
  apiRequest<{ campaign_id: string }>(
    "POST",
    `${AUTO}/campaigns/${encodeURIComponent(id)}/attach`,
    { request_id: requestId },
  );

export const getCampaignCards = (id: string) =>
  apiRequest<{ pending: PendingCardEpisode[] }>(
    "GET",
    `${AUTO}/campaigns/${encodeURIComponent(id)}/cards`,
  );

/** Say which layout card an episode had (`card` null: no card / it deviated). */
export const confirmCampaignCard = (
  id: string,
  commandId: string,
  episodeKey: string,
  card: string | null,
) =>
  apiRequest<CommandResult>(
    "POST",
    `${AUTO}/campaigns/${encodeURIComponent(id)}/cards`,
    { command_id: commandId, episode_key: episodeKey, card },
  );

export const campaignCommand = (
  id: string,
  action: "pause" | "resume" | "unblind",
  commandId: string,
) =>
  apiRequest<CommandResult>(
    "POST",
    `${AUTO}/campaigns/${encodeURIComponent(id)}/${action}`,
    { command_id: commandId, confirm: action },
  );

export const getCampaignReport = (id: string, basis?: string) =>
  apiRequest<CampaignReport>(
    "GET",
    `${AUTO}/campaigns/${encodeURIComponent(id)}/report${
      basis ? `?basis=${encodeURIComponent(basis)}` : ""
    }`,
  );

/** Make (or make again) the report of one label basis; the controller
 * writes it from the ledger. Answers `{result, basis, conclusion_level}`. */
export const generateCampaignReport = (
  id: string,
  commandId: string,
  basis?: string,
) =>
  apiRequest<{ result: string; basis?: string; conclusion_level?: string }>(
    "POST",
    `${AUTO}/campaigns/${encodeURIComponent(id)}/report`,
    basis ? { command_id: commandId, basis } : { command_id: commandId },
  );

/** The path of one report file under the proxy (a link for downloads). */
export function reportFileUrl(
  id: string,
  name: string,
  basis?: string,
): string {
  const path = name.split("/").map(encodeURIComponent).join("/");
  return `/api/levi/${AUTO}/campaigns/${encodeURIComponent(id)}/report/files/${path}${
    basis ? `?basis=${encodeURIComponent(basis)}` : ""
  }`;
}

export async function getReportFileJson(
  id: string,
  name: string,
  basis?: string,
): Promise<unknown> {
  const response = await fetch(reportFileUrl(id, name, basis), {
    cache: "no-store",
  });
  if (!response.ok) throw await errorOf(response);
  return response.json();
}

export async function getReportFileText(
  id: string,
  name: string,
  basis?: string,
): Promise<string> {
  const response = await fetch(reportFileUrl(id, name, basis), {
    cache: "no-store",
  });
  if (!response.ok) throw await errorOf(response);
  return response.text();
}

/** The calls the wizard and campaign pages make, as one object: a page takes
 * it as a prop (default: the real calls), so a test hands in its own. */
export const wizardApi = {
  getCapabilities,
  getPolicies,
  getSetupGuide,
  createJob,
  planLaunch,
  launchRun,
  planCampaign,
  startCampaign,
  getCampaign,
  listCampaigns,
  confirmCampaign,
  attachCampaign,
  getCampaignCards,
  confirmCampaignCard,
  campaignCommand,
  getCampaignReport,
  generateCampaignReport,
  getReportFileJson,
  getReportFileText,
};
export type WizardApi = typeof wizardApi;
