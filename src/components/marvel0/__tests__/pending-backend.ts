// What this page has ahead of the backend (2026-11-19): the two yaw keys of the
// takeover and the two fields of the prepare body (`grasp_source`,
// `unattended`) are added to the backend on a parallel branch. The contract
// tests compare the page with the backend's own tables; until that branch is
// merged they would fail on exactly these names, and after it they must pass
// without being edited. So a name listed here is compared when the backend has
// it, and the backend must have all of its group or none of it (never half).
import { KEY_IDS } from "../autonomy-doc";

export const YAW_KEYS: readonly string[] = ["assist_yaw_pos", "assist_yaw_neg"];
export const PREPARE_FIELDS_AHEAD: readonly string[] = [
  "grasp_source",
  "unattended",
];

const present = (backend: readonly string[], group: readonly string[]) =>
  group.filter((name) => backend.includes(name));

/** The group's names the backend has: all of them, or none. */
export function backendHas(
  backend: readonly string[],
  group: readonly string[],
): boolean {
  const have = present(backend, group);
  if (have.length !== 0 && have.length !== group.length)
    throw new Error(
      `the backend has only part of ${group.join(", ")}: ${have.join(", ")}`,
    );
  return have.length > 0;
}

/** The page's list minus the names of a group the backend does not have yet. */
export function pageNamesFor(
  page: readonly string[],
  backend: readonly string[],
  group: readonly string[],
): string[] {
  return backendHas(backend, group)
    ? [...page]
    : page.filter((name) => !group.includes(name));
}

/** The page's key ids as the backend knows them today. */
export const pageKeysFor = (backend: readonly string[]): string[] =>
  pageNamesFor(KEY_IDS, backend, YAW_KEYS);
