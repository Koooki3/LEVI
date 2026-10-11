import type { SessionIdentity } from "./session-route";
import type {
  ResultIndex,
  ResultItem,
  ResultRun,
  ResultViewer,
} from "./result-types";
const base = "/api/levi/live/results";
export function resultSessionQuery(identity: SessionIdentity): string | null {
  if (
    !identity.root ||
    !identity.group ||
    !identity.task ||
    (!identity.run && !identity.id)
  )
    return null;
  const q = new URLSearchParams({
    root: identity.root,
    group: identity.group,
    task: identity.task,
    session: identity.run || identity.id,
  });
  if (identity.id) q.set("session_id", identity.id);
  if (identity.run) q.set("run_id", identity.run);
  return q.toString();
}
export function resultSessionPath(identity: SessionIdentity): string | null {
  const q = resultSessionQuery(identity);
  return q ? `${base}/session?${q}` : null;
}
export function resultDownloadHref(
  identity: SessionIdentity,
  format: "json" | "markdown",
  language: string,
): string | null {
  const q = resultSessionQuery(identity);
  return q
    ? `${base}/session/download?${q}&${new URLSearchParams({ format, lang: language === "zh" ? "zh" : "en" })}`
    : null;
}
export function resultIndexPath(
  dataset?: string,
  annotationRun?: string,
): string {
  const q = new URLSearchParams();
  if (dataset) q.set("dataset", dataset);
  if (annotationRun) q.set("run_id", annotationRun);
  return `${base}${q.size ? `?${q}` : ""}`;
}
export function resultMatches(
  item: ResultItem,
  identity: SessionIdentity,
): boolean {
  const i = item.identity;
  return (
    i.root === identity.root &&
    i.group === identity.group &&
    i.task === identity.task &&
    (!identity.id || i.session_id === identity.id) &&
    (!identity.run || i.run_id === identity.run) &&
    (!identity.dataset ||
      item.result_visibility !== "visible" ||
      i.datasets.includes(identity.dataset))
  );
}
export function canShowResult(item: ResultItem): boolean {
  return (
    item.source?.kind === "current_session" &&
    item.result_visibility === "visible" &&
    ["waiting_annotation", "partial", "reported", "stale"].includes(item.status)
  );
}
export function canShowStatistics(
  item: ResultItem,
  identity: SessionIdentity,
): boolean {
  return (
    item.source?.kind === "current_session" &&
    item.statistics_visibility === "visible" &&
    !["blocked", "rebound", "archived", "missing_source"].includes(
      item.status,
    ) &&
    resultMatches(item, identity) &&
    !!identity.dataset &&
    item.identity.datasets.includes(identity.dataset)
  );
}
function safeViewerURL(href: string): URL | null {
  try {
    const url = new URL(href, "http://levi.invalid");
    if (
      !["http:", "https:"].includes(url.protocol) ||
      url.username ||
      url.password
    )
      return null;
    if (
      url.origin !== "http://levi.invalid" &&
      !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)
    )
      return null;
    if (!/^\/local\/[^/]+\/episode_\d+$/.test(url.pathname)) return null;
    return url;
  } catch {
    return null;
  }
}
function episodeHref(href: string, episode: number): string | null {
  if (!Number.isInteger(episode) || episode < 0) return null;
  const url = safeViewerURL(href);
  if (!url) return null;
  url.pathname = url.pathname.replace(/episode_\d+$/, `episode_${episode}`);
  return url.origin === "http://levi.invalid"
    ? url.pathname + url.search
    : url.href;
}
export function resultEpisodeHref(
  viewer: ResultViewer | undefined,
  episode: number,
): string | null {
  return viewer?.status === "ready" && viewer.viewer_href
    ? episodeHref(viewer.viewer_href, episode)
    : null;
}
export function annotationRunHref(
  run: ResultRun,
  episode: number,
): string | null {
  if (!run.repo_id || !run.annotation_href) return null;
  const url = safeViewerURL(run.annotation_href);
  if (!url || url.searchParams.get("agent_run") !== run.run_id) return null;
  return episodeHref(run.annotation_href, episode);
}
export function linkedAnnotation(
  item: ResultItem,
  repo: string,
  run: string,
): boolean {
  if (!canShowResult(item)) return false;
  const datasets = (item.viewers ?? [])
    .filter(
      (viewer) => viewer.repo_id === repo || viewer.native_repo_id === repo,
    )
    .map((viewer) => viewer.dataset);
  return item.runs.some(
    (ref) =>
      ref.run_id === run &&
      (ref.repo_id === repo || datasets.includes(ref.dataset)),
  );
}
export async function readResult(
  path: string,
  signal: AbortSignal,
): Promise<ResultItem | ResultIndex> {
  const response = await fetch(path, { cache: "no-store", signal });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const data: unknown = await response.json();
  if (!data || typeof data !== "object")
    throw new Error("Invalid result response");
  if ("items" in data) {
    if (path.startsWith(`${base}/session?`))
      throw new Error("Invalid session result");
    const index = data as ResultIndex;
    if (
      index.schema !== "levi.live.result-index.v1" ||
      !Array.isArray(index.items)
    )
      throw new Error("Invalid result index");
    return index;
  }
  const item = data as ResultItem;
  if (
    !item.identity ||
    !["visible", "withheld", "unknown"].includes(item.result_visibility) ||
    !Array.isArray(item.identity.datasets) ||
    !Array.isArray(item.records) ||
    !Array.isArray(item.runs) ||
    !Array.isArray(item.reports) ||
    !item.comparison
  )
    throw new Error("Invalid result response");
  return item;
}
