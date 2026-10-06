// Datasets of the live evaluation workspace are linked into this LEVI
// read-only (levi/links.py): they are listed as `local/live.<name>`, can be
// opened and read, and every write is refused with a 403. What the pages
// decide from that, in one place so it can be tested without a browser.
import type { CatalogEntry } from "@/types/dataset-format.types";

/** The name prefix of a linked dataset (`levi/links.py` PREFIX). */
export const LINKED_PREFIX = "live.";

/** What the service answers (403) to a write on a linked dataset. It is also
 * the key the catalogues translate it by, so it must match the service word
 * for word (levi/links.py READ_ONLY). */
export const LINKED_READ_ONLY =
  "This dataset belongs to the live evaluation workspace and is read-only here; review and label it where the live service keeps it";

/** The short reason shown next to a control that is off because of it. */
export const LINKED_REASON =
  "Read-only: belongs to the live evaluation workspace";

/** The top-of-page sentence of the viewer. */
export const LINKED_NOTICE =
  "This is a dataset of the live evaluation workspace: read-only. Time segments, outcome labels and automatic verdicts come from the live service; to label or review, do it in the workspace where the live service keeps them.";

/** The dataset's name in the repo id `local/<name>` (other ids: null). */
function localName(repoId: string | null | undefined): string | null {
  if (!repoId || !repoId.startsWith("local/")) return null;
  return repoId.slice("local/".length);
}

/** By its id alone (before the catalog has answered): the `live.` prefix. A
 * guess made on the safe side: a page that does not know yet writes nothing. */
export function isLinkedRepo(repoId: string | null | undefined): boolean {
  return localName(repoId)?.startsWith(LINKED_PREFIX) ?? false;
}

/** By its catalog entry: what the service says is the answer; with no entry
 * yet, the id's prefix is. */
export function isLinkedDataset(
  repoId: string | null | undefined,
  entry?: Pick<CatalogEntry, "linked"> | null,
): boolean {
  if (entry) return Boolean(entry.linked);
  return isLinkedRepo(repoId);
}

/** The service's refusal of a write on a linked dataset, however it is
 * wrapped (a bare sentence, a `{"detail": …}` body, `Error: …`). */
export function isLinkedRefusal(message: string | null | undefined): boolean {
  return (message ?? "").includes(LINKED_READ_ONLY);
}

/** What a failed write shows: the refusal as its own sentence (the catalogue
 * translates it), anything else as the caller's own wording. */
export function writeFailure(message: string, own: string): string {
  return isLinkedRefusal(message) ? LINKED_READ_ONLY : own;
}
