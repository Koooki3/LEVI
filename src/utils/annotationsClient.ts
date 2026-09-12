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
  Sam3Revision,
} from "../types/object-annotation.types";

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
    const res = await fetch(endpoint("/api/health"));
    return res.ok;
  } catch {
    return false;
  }
}

export async function loadDataset(
  ident: DatasetIdent,
): Promise<{ ok: boolean }> {
  if (!ENV_URL) return { ok: false };
  const res = await fetch(endpoint("/api/dataset/load"), {
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
  const res = await fetch(buildUrl(`/api/episodes/${episodeId}/atoms`, ident));
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
  const res = await fetch(endpoint(`/api/episodes/${episodeId}/atoms`), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      episode_index: episodeId,
      repo_id: ident.repoId || null,
      local_path: ident.localPath || null,
      atoms,
    }),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => `${res.status}`);
    throw new Error(text || `save atoms: ${res.status}`);
  }
  const data = (await res.json().catch(() => ({}))) as { path?: string | null };
  return { path: data.path ?? null };
}

export async function fetchFrameTimestamps(
  episodeId: number,
  ident: DatasetIdent,
): Promise<number[]> {
  if (!ENV_URL) return [];
  const res = await fetch(
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
}> {
  if (!ENV_URL) throw new Error("Annotate backend not configured");
  const res = await fetch(endpoint("/api/export"), {
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
  const res = await fetch(endpoint("/api/push_to_hub"), {
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
  const response = await fetch(endpoint("/api/sam3/status"), {
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

export async function planSam3(
  ident: DatasetIdent,
  plan: Omit<Sam3Plan, "repo_id" | "local_path" | "revision">,
): Promise<Sam3Plan & { plan_id: string; status: string }> {
  if (!ENV_URL) throw new Error("Annotate backend not configured");
  const response = await fetch(endpoint("/api/sam3/plan"), {
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
  const response = await fetch(endpoint("/api/sam3/run"), {
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
  const response = await fetch(buildUrl("/api/sam3/revisions", ident), {
    cache: "no-store",
  });
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
  const response = await fetch(url, { cache: "no-store" });
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
  const response = await fetch(buildUrl("/api/sam3/edits", ident), {
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
  const response = await fetch(buildUrl("/api/sam3/jobs/" + jobId, ident), {
    cache: "no-store",
  });
  if (!response.ok) throw new Error("SAM3 job: " + response.status);
  return response.json() as Promise<Sam3JobStatus>;
}

export async function cancelSam3Job(
  jobId: string,
  ident: DatasetIdent,
): Promise<Record<string, unknown>> {
  if (!ENV_URL) throw new Error("Annotate backend not configured");
  const response = await fetch(
    buildUrl(`/api/sam3/jobs/${jobId}/cancel`, ident),
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    },
  );
  if (!response.ok) throw new Error(`SAM3 cancel: ${response.status}`);
  return response.json();
}
