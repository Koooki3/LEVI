// Reading loosely typed JSON, shared by the parts of the Rig console that
// read the policy-server and command routes. A value of the wrong type is
// null (or an empty list), never a guess.
export type Rec = Record<string, unknown>;

export const rec = (value: unknown): Rec | null =>
  typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Rec)
    : null;
export const str = (value: unknown, max = 300): string | null =>
  typeof value === "string" && value.trim() ? value.trim().slice(0, max) : null;
export const num = (value: unknown): number | null =>
  typeof value === "number" && Number.isFinite(value) ? value : null;
export const bool = (value: unknown): boolean | null =>
  typeof value === "boolean" ? value : null;
export const list = (value: unknown): unknown[] =>
  Array.isArray(value) ? value : [];

/** A piece of server text for the page: the page's own words when it knows
 * what the server said (`say`, a `Say` of rig-logic.ts), else the server's
 * sentence as written, which is English whatever the page's language and is
 * marked so (`lang="en"`). */
export interface Worded<S> {
  say: S | null;
  text: string;
}
