/**
 * The Agent Workbench's request helpers and the shapes it reads from
 * `/api/levi/agent/v1`. Moved verbatim out of agent-workbench.tsx.
 */
import type { HarnessPlan } from "./agent-plan";
import type { Connection } from "./agent-connections";

export const base = "/api/levi/agent/v1";
export async function api<T>(path: string, value?: unknown): Promise<T> {
  const response = await fetch(
    base + path,
    value === undefined
      ? { cache: "no-store" }
      : {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(value),
        },
  );
  const data = await response.json();
  if (!response.ok)
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : JSON.stringify(data.detail),
    );
  return data;
}
export function tool<T>(name: string, args: unknown, key?: string) {
  return api<T>("/tools", { name, arguments: args, idempotency_key: key });
}
export type Provider = Connection;
export type ProviderKind = NonNullable<Connection["kind"]>;
export type Run = {
  plan?: HarnessPlan;
  cache_hits?: number;
  id: string;
  status: string;
  context: {
    repo_id: string;
    episodes: number[];
    cameras: string[];
    instruction: string;
    provider: string;
    supervision?: "none" | "shadow" | "supervised";
    pilot_runtime?: "codex" | "claude";
    workflow?: { kind: string };
    budget?: unknown;
    allow_media_egress?: boolean;
  };
  completed: number[];
  snapshot_bytes: number;
  requests: number;
  tokens: number;
  reserved_tokens: number;
  reason?: string;
  changes?: string;
};
export type Proposal = {
  episode_index: number;
  kind: string;
  content: string;
  start: number;
  end: number | null;
  evidence_ids: string[];
};
export type Change = {
  object_jobs: string[];
  undo_of?: string;
  decisions: Record<string, string>;
  id: string;
  revision: number;
  status: string;
  proposals: Proposal[];
  base_revision: string;
  provenance: { coverage: string };
};
export type Evidence = {
  id: string;
  episode_index: number;
  camera_key?: string;
  timestamp: number;
  frame_index: number;
  artifact?: string;
};
