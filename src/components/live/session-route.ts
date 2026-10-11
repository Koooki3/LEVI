// A bookmark carries the source identity, never a list index or display label.
import type { LiveSession } from "./types";
import type { StatsSession } from "./stats-logic";

export interface SessionIdentity {
  root: string;
  group: string;
  task: string;
  id: string;
  run: string;
  dataset: string;
}
export function identityOf(s: LiveSession): SessionIdentity | null {
  if (!s.session_id && !s.run_id) return null;
  return {
    root: s.root ?? "",
    group: s.group,
    task: s.task_folder,
    id: s.session_id ?? "",
    run: s.run_id ?? "",
    dataset: s.dataset ?? "",
  };
}
export function sessionHref(s: LiveSession): string | null {
  const identity = identityOf(s);
  return identity ? identityHref(identity) : null;
}
function identityHref(identity: SessionIdentity): string {
  return `/live/sessions/${encodeURIComponent(JSON.stringify(identity))}`;
}
export function statsSessionHref(s: StatsSession): string | null {
  return s.dataset && s.session
    ? identityHref({
        root: "",
        group: "",
        task: "",
        id: "",
        run: s.session,
        dataset: s.dataset,
      })
    : null;
}
export function parseSessionIdentity(raw: string): SessionIdentity | null {
  if (raw.length > 12000) return null;
  try {
    let value: SessionIdentity;
    try {
      value = JSON.parse(raw) as SessionIdentity;
    } catch {
      value = JSON.parse(decodeURIComponent(raw)) as SessionIdentity;
    }
    // Client navigation and direct loads can expose different encodings.
    // Decode only after parsing fails, preserving literal percent signs.
    if (
      !value ||
      !["root", "group", "task", "id", "run", "dataset"].every(
        (key) => typeof value[key as keyof SessionIdentity] === "string",
      )
    )
      return null;
    if (!value.id && !value.run) return null;
    if ((!value.group || !value.task) && !value.dataset) return null;
    return value;
  } catch {
    return null;
  }
}
export function findSession(
  identity: SessionIdentity,
  sessions: LiveSession[],
): LiveSession | null {
  const found = sessions.filter((s) => {
    if (identity.group && identity.task)
      return (
        (s.root ?? "") === identity.root &&
        s.group === identity.group &&
        s.task_folder === identity.task &&
        (s.session_id ?? "") === identity.id &&
        (s.run_id ?? "") === identity.run
      );
    return (
      s.dataset === identity.dataset &&
      (s.run_id || s.session_id) === identity.run
    );
  });
  // Ambiguous older records must never select somebody else's session.
  return found.length === 1 ? found[0] : null;
}
