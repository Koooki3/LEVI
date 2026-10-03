// Removing an episode from a live dataset and restoring it (docs/LIVE.md,
// "Removing an episode"). A soft delete: only the service's own state changes,
// never a rollout folder or the mirror. The calls are a person's: the page's
// proxy attaches the UI token, an agent's credential is refused by the core.
import { leviRequest } from "@/components/levi-api";
import type { ChangeResult } from "./types";

export type Requester = <T>(
  method: "POST",
  path: string,
  body?: unknown,
) => Promise<T>;

const base = (dataset: string) =>
  `live/datasets/${encodeURIComponent(dataset)}`;

export function removeEpisodes(
  dataset: string,
  demos: string[],
  reason: string,
  request: Requester = leviRequest,
): Promise<ChangeResult> {
  return request<ChangeResult>("POST", `${base(dataset)}/exclude`, {
    demos,
    reason: reason.trim(),
  });
}

export function restoreEpisodes(
  dataset: string,
  demos: string[],
  request: Requester = leviRequest,
): Promise<ChangeResult> {
  return request<ChangeResult>("POST", `${base(dataset)}/restore`, { demos });
}

/** One sentence for what a call did, translated by `t`. */
export function changeNotice(
  kind: "remove" | "restore",
  result: ChangeResult,
  t: (text: string) => string,
): string {
  const fill = (text: string, n: number) => t(text).replace("{n}", String(n));
  const parts = [
    fill(
      kind === "remove"
        ? "{n} episode(s) removed (restorable)"
        : "{n} episode(s) restored",
      result.changed.length,
    ),
  ];
  if (result.unchanged.length > 0)
    parts.push(
      fill(
        kind === "remove" ? "{n} already removed" : "{n} were not removed",
        result.unchanged.length,
      ),
    );
  if (result.cancelled_runs?.length)
    parts.push(
      fill("{n} review run(s) cancelled", result.cancelled_runs.length),
    );
  return parts.join(" · ");
}

export type Outcome =
  | { ok: true; notice: string; result: ChangeResult }
  | { ok: false; message: string };

/** What happens when a person confirms a removal or presses Restore: one
 * call for all the episodes, then a sentence; a refusal (the core's 404 or
 * 409) comes back as its message, with nothing changed. */
export async function applyChange(
  kind: "remove" | "restore",
  dataset: string,
  demos: string[],
  reason: string,
  t: (text: string) => string,
  request: Requester = leviRequest,
): Promise<Outcome> {
  try {
    const result =
      kind === "remove"
        ? await removeEpisodes(dataset, demos, reason, request)
        : await restoreEpisodes(dataset, demos, request);
    return { ok: true, notice: changeNotice(kind, result, t), result };
  } catch (e) {
    return { ok: false, message: e instanceof Error ? e.message : String(e) };
  }
}
