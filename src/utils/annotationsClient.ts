// Modified for LEVI (2026); see NOTICE and docs/UPSTREAM.md.
/**
 * Client for the FastAPI annotation backend in `backend/`.
 *
 * The backend URL is configured via the `NEXT_PUBLIC_ANNOTATE_BACKEND_URL`
 * env var so it can be statically substituted by Next.js. When unset, all
 * annotation write paths are disabled and the UI falls back to sessionStorage
 * for read/edit only.
 */

import type { LanguageAtom } from "../types/language.types";
import type {
  ObjectAnnotation,
  Sam3Capabilities,
  Sam3Edit,
  Sam3JobStatus,
  Sam3Plan,
  Sam3PromptPreset,
  Sam3Revision,
} from "../types/object-annotation.types";

// Revision tokens belong to the editor's last read, never an automatic pre-save
// refresh (which would hide concurrent edits). Kept in memory, not credentials.
const annotationRevisions = new Map<string, string>();
async function annotationFetch(
  input: string | URL,
  init?: RequestInit,
): Promise<Response> {
  const url = new URL(input, window.location.origin);
  let body: Record<string, unknown> = {};
  if (typeof init?.body === "string") {
    try {
      body = JSON.parse(init.body);
    } catch {
      /* transport handles bad input */
    }
  }
  const dataset =
    body.repo_id ||
    body.local_path ||
    url.searchParams.get("repo_id") ||
    url.searchParams.get("local_path");
  const path = url.pathname;
  const channel = path.includes("/sam3/")
    ? "objects"
    : path.includes("outcome")
      ? "outcomes"
      : path;
  const key = `${dataset}:${channel}`;
  const headers = new Headers(init?.headers);
  const revision = annotationRevisions.get(key);
  if (revision && init?.method && init.method !== "GET")
    headers.set("x-levi-annotation-revision", revision);
  const response = await fetch(input, { ...init, headers });
  const observed = response.headers.get("x-levi-annotation-revision");
  if (dataset && response.ok && observed)
    annotationRevisions.set(key, observed);
  return response;
}

const ENV_URL = "LEVI";
function endpoint(path: string): string {
  return new URL(
    "/api/annotation/" + path.replace(/^\/api\//, ""),
    window.location.origin,
  ).toString();
}

export function isAnnotateBackendEnabled(): boolean {
  return !!ENV_URL;
}

export function getAnnotateBackendUrl(): string | null {
  return ENV_URL;
}

export interface DatasetIdent {
  repoId?: string | null;
  localPath?: string | null;
  revision?: string | null;
}

function buildUrl(path: string, ident: DatasetIdent): string {
  if (!ENV_URL) throw new Error("Annotate backend not configured");
  const url = new URL(endpoint(path));
  if (ident.repoId) url.searchParams.set("repo_id", ident.repoId);
  if (ident.revision) url.searchParams.set("revision", ident.revision);
  if (ident.localPath) url.searchParams.set("local_path", ident.localPath);
  return url.toString();
}

export async function pingBackend(): Promise<boolean> {
  if (!ENV_URL) return false;
  try {
    const res = await annotationFetch(endpoint("/api/health"));
    return res.ok;
  } catch {
    return false;
  }
}

export async function loadDataset(
  ident: DatasetIdent,
): Promise<{ ok: boolean }> {
  if (!ENV_URL) return { ok: false };
  const res = await annotationFetch(endpoint("/api/dataset/load"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      repo_id: ident.repoId || null,
      revision: ident.revision || null,
      local_path: ident.localPath || null,
    }),
  });
  return { ok: res.ok };
}

export async function fetchEpisodeAtoms(
  episodeId: number,
  ident: DatasetIdent,
): Promise<LanguageAtom[]> {
  if (!ENV_URL) return [];
  await loadDataset(ident);
  const res = await annotationFetch(
    buildUrl(`/api/episodes/${episodeId}/atoms`, ident),
  );
  if (!res.ok) {
    throw new Error(`fetch atoms: ${res.status}`);
  }
  const data = (await res.json()) as { atoms?: LanguageAtom[] };
  return data.atoms || [];
}

export async function saveEpisodeAtoms(
  episodeId: number,
  ident: DatasetIdent,
  atoms: LanguageAtom[],
): Promise<{ path: string | null }> {
  if (!ENV_URL) return { path: null };
  const res = await annotationFetch(
    endpoint(`/api/episodes/${episodeId}/atoms`),
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        episode_index: episodeId,
        repo_id: ident.repoId || null,
        local_path: ident.localPath || null,
        atoms,
      }),
    },
  );
  if (!res.ok) {
    const text = await res.text().catch(() => `${res.status}`);
    throw new Error(text || `save atoms: ${res.status}`);
  }
  const data = (await res.json().catch(() => ({}))) as { path?: string | null };
  return { path: data.path ?? null };
}

/**
 * Delete an episode's annotation file entirely — distinct from saving an
 * empty atoms list (which still records "reviewed, nothing to annotate").
 * Reverts the episode to its pristine, never-annotated state.
 */
export async function deleteEpisodeAtoms(
  episodeId: number,
  ident: DatasetIdent,
): Promise<{ deleted: boolean }> {
  if (!ENV_URL) return { deleted: false };
  const res = await annotationFetch(
    buildUrl(`/api/episodes/${episodeId}/atoms`, ident),
    {
      method: "DELETE",
    },
  );
  if (!res.ok) {
    const text = await res.text().catch(() => `${res.status}`);
    throw new Error(text || `delete atoms: ${res.status}`);
  }
  const data = (await res.json().catch(() => ({}))) as { deleted?: boolean };
  return { deleted: !!data.deleted };
}

export interface AnnotationSummary {
  /** `episode_index` → has non-empty language_persistent/language_events atoms. */
  language: Record<string, boolean>;
  /** `episode_index` → has any published SAM3 object/track annotation. */
  vision: Record<string, boolean>;
}

/** Powers the sidebar's per-episode annotated/unannotated indicator dots. */
export async function fetchAnnotationSummary(
  ident: DatasetIdent,
): Promise<AnnotationSummary> {
  if (!ENV_URL) return { language: {}, vision: {} };
  const res = await annotationFetch(
    buildUrl("/api/episodes/annotation-summary", ident),
  );
  if (!res.ok) return { language: {}, vision: {} };
  const data = (await res.json()) as Partial<AnnotationSummary>;
  return { language: data.language || {}, vision: data.vision || {} };
}

export type OutcomeValue = "success" | "failure";

export interface OutcomeLabel {
  outcome: OutcomeValue;
  source: "human";
  updated_at?: string;
}

/** Human success/failure labels (override the dataset's `levi_outcome`). */
export async function fetchOutcomeLabels(
  ident: DatasetIdent,
): Promise<Record<string, OutcomeLabel>> {
  if (!ENV_URL) return {};
  const res = await annotationFetch(buildUrl("/api/episodes/outcomes", ident), {
    cache: "no-store",
  });
  if (!res.ok) return {};
  const data = (await res.json()) as { labels?: Record<string, OutcomeLabel> };
  return data.labels || {};
}

/** Set, or with `null` clear, one episode's human outcome label. */
export async function saveOutcomeLabel(
  episodeId: number,
  ident: DatasetIdent,
  outcome: OutcomeValue | null,
): Promise<void> {
  if (!ENV_URL) throw new Error("Annotate backend not configured");
  const res = await annotationFetch(
    endpoint(`/api/episodes/${episodeId}/outcome`),
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        repo_id: ident.repoId || null,
        local_path: ident.localPath || null,
        outcome,
      }),
    },
  );
  if (!res.ok) {
    const text = await res.text().catch(() => `${res.status}`);
    throw new Error(text || `save outcome: ${res.status}`);
  }
}

async function postJson<R>(path: string, body: unknown): Promise<R> {
  if (!ENV_URL) throw new Error("Annotate backend not configured");
  const res = await annotationFetch(endpoint(path), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = `${res.status}`;
    try {
      const data = (await res.json()) as { detail?: unknown };
      if (typeof data.detail === "string") detail = data.detail;
    } catch {
      /* keep the status */
    }
    throw new Error(detail);
  }
  return (await res.json()) as R;
}

function identBody(ident: DatasetIdent) {
  return { repo_id: ident.repoId || null, local_path: ident.localPath || null };
}

export interface EpisodeStatus {
  episode_index: number;
  done: true;
  by: string;
  confirmed_at: number;
}

/** Episodes a person confirmed as completely annotated. */
export async function fetchEpisodeStatus(
  ident: DatasetIdent,
): Promise<Record<string, EpisodeStatus>> {
  if (!ENV_URL) return {};
  const res = await annotationFetch(buildUrl("/api/episodes/status", ident), {
    cache: "no-store",
  });
  if (!res.ok) return {};
  const data = (await res.json()) as { status?: Record<string, EpisodeStatus> };
  return data.status || {};
}

/** Confirm (or undo the confirmation) that an episode is complete. */
export async function saveEpisodeStatus(
  episodeId: number,
  ident: DatasetIdent,
  done: boolean,
): Promise<EpisodeStatus | null> {
  const data = await postJson<{ status: EpisodeStatus | null }>(
    `/api/episodes/${episodeId}/status`,
    { ...identBody(ident), done },
  );
  return data.status;
}

export interface SubtaskTerm {
  id: string;
  label: string;
  definition?: string;
}

export interface Vocabulary {
  subtasks: SubtaskTerm[];
  updated_at: number | null;
  /** The latest agent plan's definitions, offered when there is none. */
  suggested?: SubtaskTerm[];
  /** Ids every subtask track accepts besides the defined ones. */
  special?: string[];
}

export async function fetchVocabulary(
  ident: DatasetIdent,
): Promise<Vocabulary> {
  if (!ENV_URL) return { subtasks: [], updated_at: null };
  const res = await annotationFetch(
    buildUrl("/api/dataset/vocabulary", ident),
    {
      cache: "no-store",
    },
  );
  if (!res.ok) return { subtasks: [], updated_at: null };
  return (await res.json()) as Vocabulary;
}

export async function saveVocabulary(
  ident: DatasetIdent,
  subtasks: SubtaskTerm[],
): Promise<Vocabulary> {
  return postJson<Vocabulary>("/api/dataset/vocabulary", {
    ...identBody(ident),
    subtasks,
  });
}

export interface RecordingSession {
  dataset: string;
  started_at: number;
  by: string;
}

export interface RecordingResult {
  path: string;
  seconds: number;
  episodes: number;
  segments: number;
  coverage_seconds: number;
  coverage_frames: number;
}

export async function fetchRecording(
  ident: DatasetIdent,
): Promise<RecordingSession | null> {
  if (!ENV_URL) return null;
  const res = await annotationFetch(buildUrl("/api/eval/recording", ident), {
    cache: "no-store",
  });
  if (!res.ok) return null;
  const data = (await res.json()) as { session?: RecordingSession | null };
  return data.session || null;
}

export async function startRecording(
  ident: DatasetIdent,
): Promise<RecordingSession> {
  const data = await postJson<{ session: RecordingSession }>(
    "/api/eval/recording/start",
    identBody(ident),
  );
  return data.session;
}

export async function stopRecording(
  ident: DatasetIdent,
): Promise<RecordingResult> {
  return postJson<RecordingResult>(
    "/api/eval/recording/stop",
    identBody(ident),
  );
}

export async function cancelRecording(ident: DatasetIdent): Promise<void> {
  await postJson("/api/eval/recording/cancel", identBody(ident));
}

export async function fetchFrameTimestamps(
  episodeId: number,
  ident: DatasetIdent,
): Promise<number[]> {
  if (!ENV_URL) return [];
  const res = await annotationFetch(
    buildUrl(`/api/episodes/${episodeId}/frame_timestamps`, ident),
  );
  if (!res.ok) return [];
  const data = (await res.json()) as { timestamps?: number[] };
  return data.timestamps || [];
}

export async function exportDataset(
  ident: DatasetIdent,
  outputDir?: string | null,
  copyVideos = false,
): Promise<{
  output_dir: string;
  persistent_rows: number;
  event_rows: number;
  reused_existing_export: boolean;
}> {
  if (!ENV_URL) throw new Error("Annotate backend not configured");
  const res = await annotationFetch(endpoint("/api/export"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      repo_id: ident.repoId || null,
      revision: ident.revision || null,
      local_path: ident.localPath || null,
      output_dir: outputDir || null,
      copy_videos: !!copyVideos,
    }),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => `${res.status}`);
    throw new Error(text || `export: ${res.status}`);
  }
  return res.json();
}

export interface PushToHubResult {
  ok: boolean;
  repo_id: string;
  url: string;
  message: string;
}

export async function pushToHub(
  ident: DatasetIdent,
  hfToken: string,
  pushInPlace: boolean,
  newRepoId: string | null,
  privateRepo: boolean,
  commitMessage: string,
): Promise<PushToHubResult> {
  if (!ENV_URL) throw new Error("Annotate backend not configured");
  const res = await annotationFetch(endpoint("/api/push_to_hub"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      repo_id: ident.repoId || null,
      revision: ident.revision || null,
      local_path: ident.localPath || null,
      hf_token: hfToken,
      push_in_place: pushInPlace,
      new_repo_id: newRepoId || null,
      private: privateRepo,
      commit_message: commitMessage,
    }),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => `${res.status}`);
    throw new Error(text || `push: ${res.status}`);
  }
  return res.json();
}

export async function getSam3Status(): Promise<Sam3Capabilities> {
  const response = await annotationFetch(endpoint("/api/sam3/status"), {
    cache: "no-store",
  });
  if (!response.ok) throw new Error("SAM3 status: " + response.status);
  return response.json() as Promise<Sam3Capabilities>;
}

export async function getSam3Capabilities(): Promise<Sam3Capabilities> {
  // Keep the old client name for extensions while using the richer global
  // status response.
  return getSam3Status();
}

export async function startSam3CheckpointDownload(): Promise<
  Sam3Capabilities & { download_started?: boolean }
> {
  const response = await annotationFetch(
    endpoint("/api/sam3/checkpoint/download"),
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    },
  );
  if (!response.ok) {
    const text = await response.text().catch(() => `${response.status}`);
    let detail = "";
    try {
      const payload = JSON.parse(text) as { detail?: unknown };
      if (typeof payload.detail === "string") detail = payload.detail.trim();
    } catch {
      // Keep the raw body as a fallback for non-JSON proxy errors.
    }
    throw new Error(
      detail || text || `SAM3 checkpoint download: ${response.status}`,
    );
  }
  return response.json();
}

export async function planSam3(
  ident: DatasetIdent,
  plan: Omit<Sam3Plan, "repo_id" | "local_path" | "revision">,
): Promise<Sam3Plan & { plan_id: string; status: string }> {
  if (!ENV_URL) throw new Error("Annotate backend not configured");
  const response = await annotationFetch(endpoint("/api/sam3/plan"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      ...plan,
      repo_id: ident.repoId || null,
      local_path: ident.localPath || null,
      revision: ident.revision || null,
    }),
  });
  if (!response.ok) {
    const text = await response.text().catch(() => `${response.status}`);
    throw new Error(text || `SAM3 plan: ${response.status}`);
  }
  return response.json();
}

export async function runSam3(
  ident: DatasetIdent,
  plan: Omit<Sam3Plan, "repo_id" | "local_path" | "revision">,
): Promise<{
  ok: boolean;
  provider: "fake" | "sam3";
  revision_id?: string;
  count?: number;
  status?: string;
  job_id?: string;
}> {
  if (!ENV_URL) throw new Error("Annotate backend not configured");
  const response = await annotationFetch(endpoint("/api/sam3/run"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      ...plan,
      repo_id: ident.repoId || null,
      local_path: ident.localPath || null,
      revision: ident.revision || null,
    }),
  });
  if (!response.ok) {
    const text = await response.text().catch(() => `${response.status}`);
    throw new Error(text || `SAM3 run: ${response.status}`);
  }
  return response.json();
}

export async function fetchSam3Revisions(
  ident: DatasetIdent,
): Promise<{ current: string | null; revisions: Sam3Revision[] }> {
  if (!ENV_URL) return { current: null, revisions: [] };
  const response = await annotationFetch(
    buildUrl("/api/sam3/revisions", ident),
    {
      cache: "no-store",
    },
  );
  if (!response.ok) throw new Error(`SAM3 revisions: ${response.status}`);
  return response.json();
}

export async function fetchObjectAnnotations(
  episodeId: number,
  ident: DatasetIdent,
  options: {
    cameraKey?: string;
    frameIndex?: number;
    annotationRevision?: string;
  } = {},
): Promise<{ revision: string | null; objects: ObjectAnnotation[] }> {
  if (!ENV_URL) return { revision: null, objects: [] };
  const url = new URL(
    buildUrl(`/api/sam3/episodes/${episodeId}/objects`, ident),
  );
  if (options.cameraKey) url.searchParams.set("camera_key", options.cameraKey);
  if (options.frameIndex !== undefined)
    url.searchParams.set("frame_index", String(options.frameIndex));
  if (options.annotationRevision)
    url.searchParams.set("annotation_revision", options.annotationRevision);
  const response = await annotationFetch(url, { cache: "no-store" });
  if (!response.ok) throw new Error(`SAM3 objects: ${response.status}`);
  const data = (await response.json()) as {
    revision?: string | null;
    objects?: ObjectAnnotation[];
  };
  return { revision: data.revision ?? null, objects: data.objects ?? [] };
}

export async function editObjectAnnotation(
  ident: DatasetIdent,
  edit: Sam3Edit,
): Promise<{ ok: boolean; revision_id: string; annotation_count: number }> {
  if (!ENV_URL) throw new Error("Annotate backend not configured");
  const response = await annotationFetch(buildUrl("/api/sam3/edits", ident), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(edit),
  });
  if (!response.ok) {
    const text = await response.text().catch(() => `${response.status}`);
    throw new Error(text || `SAM3 edit: ${response.status}`);
  }
  return response.json();
}

export async function fetchSam3Job(
  jobId: string,
  ident: DatasetIdent,
): Promise<Sam3JobStatus> {
  if (!ENV_URL) throw new Error("Annotate backend not configured");
  const response = await annotationFetch(
    buildUrl("/api/sam3/jobs/" + jobId, ident),
    {
      cache: "no-store",
    },
  );
  if (!response.ok) throw new Error("SAM3 job: " + response.status);
  return response.json() as Promise<Sam3JobStatus>;
}

export async function cancelSam3Job(
  jobId: string,
  ident: DatasetIdent,
): Promise<Record<string, unknown>> {
  if (!ENV_URL) throw new Error("Annotate backend not configured");
  const response = await annotationFetch(
    buildUrl(`/api/sam3/jobs/${jobId}/cancel`, ident),
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    },
  );
  if (!response.ok) throw new Error(`SAM3 cancel: ${response.status}`);
  return response.json();
}

// Prompt presets are workspace-scoped (reusable across every dataset), not
// tied to one dataset's DatasetIdent like the endpoints above.

export async function fetchSam3PromptPresets(): Promise<Sam3PromptPreset[]> {
  if (!ENV_URL) return [];
  const response = await annotationFetch(endpoint("/api/sam3/prompt-presets"), {
    cache: "no-store",
  });
  if (!response.ok) throw new Error(`SAM3 prompt presets: ${response.status}`);
  const data = (await response.json()) as { presets?: Sam3PromptPreset[] };
  return data.presets ?? [];
}

export async function saveSam3PromptPreset(
  preset: Sam3PromptPreset,
): Promise<Sam3PromptPreset[]> {
  if (!ENV_URL) throw new Error("Annotate backend not configured");
  const response = await annotationFetch(endpoint("/api/sam3/prompt-presets"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(preset),
  });
  if (!response.ok) {
    const text = await response.text().catch(() => `${response.status}`);
    throw new Error(text || `SAM3 save preset: ${response.status}`);
  }
  const data = (await response.json()) as { presets?: Sam3PromptPreset[] };
  return data.presets ?? [];
}

export async function deleteSam3PromptPreset(
  name: string,
): Promise<Sam3PromptPreset[]> {
  if (!ENV_URL) throw new Error("Annotate backend not configured");
  const response = await annotationFetch(
    endpoint(`/api/sam3/prompt-presets/${encodeURIComponent(name)}`),
    { method: "DELETE" },
  );
  if (!response.ok) throw new Error(`SAM3 delete preset: ${response.status}`);
  const data = (await response.json()) as { presets?: Sam3PromptPreset[] };
  return data.presets ?? [];
}
