/**
 * Where you were: the episodes and datasets opened in this browser, for the
 * home page's "Continue" and "Recent datasets". Kept in localStorage only
 * (per browser, never sent anywhere); no API. The frame records a visit
 * whenever the address is an episode page (`/{org}/{dataset}/episode_{n}`)
 * or a dataset page (`/{org}/{dataset}`).
 */
import {
  readBrowserStorage,
  writeBrowserStorage,
} from "@/utils/browserStorage";

export const RECENT_KEY = "levi-recent";
export const RECENT_LIMIT = 8;

export type RecentVisit = {
  /** `org/dataset`, as in the address. */
  repo: string;
  /** Episode index, when an episode page was open. */
  episode: number | null;
  /** Epoch milliseconds. */
  at: number;
};

/** One part of a dataset id: a word character first (no "." or ".."). */
const PART = /^\w[\w.-]*$/;
const REPO = /^\w[\w.-]*\/\w[\w.-]*$/;

/** First path parts that are pages of their own, never a dataset org. */
const PAGES = new Set([
  "api",
  "design",
  "explore",
  "guide",
  "live",
  "pool",
  "report",
  "workbench",
  "_next",
]);

/** The dataset (and episode) an address shows, or null. */
export function visitFromPath(
  pathname: string,
): Omit<RecentVisit, "at"> | null {
  const parts = pathname.split("/").filter(Boolean).map(decodeSafe);
  if (parts.length < 2 || parts.length > 3) return null;
  const [org, dataset, third] = parts;
  if (PAGES.has(org) || !PART.test(org) || !PART.test(dataset)) return null;
  if (third === undefined) return { repo: `${org}/${dataset}`, episode: null };
  const match = /^episode_(\d+)$/.exec(third);
  if (!match) return null;
  return { repo: `${org}/${dataset}`, episode: Number(match[1]) };
}

function decodeSafe(part: string): string {
  try {
    return decodeURIComponent(part);
  } catch {
    return part;
  }
}

/** Newest first, one entry per dataset (its latest episode kept). */
export function addVisit(
  list: RecentVisit[],
  visit: Omit<RecentVisit, "at">,
  at: number,
): RecentVisit[] {
  const previous = list.find((item) => item.repo === visit.repo);
  const entry: RecentVisit = {
    repo: visit.repo,
    // Opening a dataset's own page keeps the episode last seen in it.
    episode: visit.episode ?? previous?.episode ?? null,
    at,
  };
  return [entry, ...list.filter((item) => item.repo !== visit.repo)].slice(
    0,
    RECENT_LIMIT,
  );
}

export function parseRecent(raw: string | null): RecentVisit[] {
  if (!raw) return [];
  try {
    const value: unknown = JSON.parse(raw);
    if (!Array.isArray(value)) return [];
    return value
      .filter(
        (item): item is RecentVisit =>
          !!item &&
          typeof item === "object" &&
          typeof item.repo === "string" &&
          REPO.test(item.repo) &&
          (item.episode === null ||
            (Number.isInteger(item.episode) && item.episode >= 0)) &&
          typeof item.at === "number",
      )
      .slice(0, RECENT_LIMIT);
  } catch {
    return [];
  }
}

export function readRecent(): RecentVisit[] {
  return parseRecent(readBrowserStorage("local", RECENT_KEY));
}

export function recordVisit(pathname: string, now = Date.now()): void {
  const visit = visitFromPath(pathname);
  if (!visit) return;
  writeBrowserStorage(
    "local",
    RECENT_KEY,
    JSON.stringify(addVisit(readRecent(), visit, now)),
  );
}

/** The address that reopens a visit. */
export function visitHref(visit: Pick<RecentVisit, "repo" | "episode">) {
  return visit.episode === null
    ? `/${visit.repo}`
    : `/${visit.repo}/episode_${visit.episode}`;
}
