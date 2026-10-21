// Backend codes in words, for the arm stack controls and the hosted run: a
// blocker or a reason arrives as `{code, level, text, params}` (or a bare
// code). The page words the codes it knows from the catalogue (`marvel0.ctl.*`
// and `marvel0.run.*`, with the params as the sentence's placeholders) and
// shows the server's own English sentence, marked as such, for a code it does
// not know. It never makes up a reason.
import en from "@/i18n/en.json";
import { list, num, rec, str, type Worded } from "./marvel0-json";
import type { Say } from "./marvel0-logic";

export type Known = ReadonlySet<string>;

/** Every `{placeholder}` of the catalogue sentence has a fact to fill it. */
function fillable(key: string, params: Record<string, string | number>) {
  const text = (en as Record<string, string>)[key] ?? "";
  return [...text.matchAll(/\{(\w+)\}/g)].every((m) => m[1] in params);
}

/** The params of a coded item as sentence placeholders: numbers and short
 * texts, anything else is dropped (a sentence never prints an object). */
export function paramsOf(raw: unknown): Record<string, string | number> {
  const out: Record<string, string | number> = {};
  for (const [name, value] of Object.entries(rec(raw) ?? {})) {
    const n = num(value);
    const s = str(value, 120);
    if (n !== null) out[name] = n;
    else if (s !== null) out[name] = s;
  }
  return out;
}

/** A coded piece of server text in the page's words when `<prefix><code>` is
 * in the catalogue for the first of the prefixes that has it, else the
 * server's sentence. */
export function codedSay(
  prefixes: string | readonly string[],
  code: string | null,
  text: string,
  params: Record<string, string | number>,
  known: Known,
): Worded<Say> {
  if (code)
    for (const prefix of typeof prefixes === "string" ? [prefixes] : prefixes) {
      const key = `${prefix}${code}`;
      // A sentence whose facts the backend did not send cannot be worded: the
      // server's own sentence says it instead of a bare {placeholder}.
      if (known.has(key) && fillable(key, params))
        return {
          say: Object.keys(params).length ? { key, params } : { key },
          text,
        };
    }
  return { say: null, text };
}

export interface Blocker {
  code: string;
  /** `required` stops the action; `warn` is shown and never stops it. */
  level: string;
  worded: Worded<Say>;
  params: Record<string, string | number>;
}

/** The sentences of the blockers: the controls' own, then the monitor's and
 * the marker's of the S4-0 card (short refusals of the same codes). */
export const BLOCK_PREFIXES = ["marvel0.ctl.block.", "marvel0.stack.refuse."];

/** The blockers of one control, as the backend lists them
 * (`start_blockers`, `stop_blockers`, `blockers`): a list of `{code, level,
 * text, params}`, or of bare codes. */
export function blockersOf(raw: unknown, known: Known): Blocker[] {
  const seen = new Set<string>();
  return list(raw).flatMap((entry): Blocker[] => {
    const item = rec(entry);
    const code = item ? str(item.code, 80) : str(entry, 80);
    // The same blocker twice (two layers name it) is said once.
    if (!code || seen.has(code)) return [];
    seen.add(code);
    const params = paramsOf(item?.params);
    return [
      {
        code,
        level: str(item?.level, 20) ?? "required",
        worded: codedSay(
          BLOCK_PREFIXES,
          code,
          str(item?.text, 400) ?? code,
          params,
          known,
        ),
        params,
      },
    ];
  });
}

/** The blockers that stop the action (a warning does not). */
export const stopping = (blockers: Blocker[]): Blocker[] =>
  blockers.filter((b) => b.level !== "warn");

// -- words by a code the backend names

const CATALOGUE = new Set(Object.keys(en));

/** The catalogue word of `<prefix><code>`, or the code as the backend wrote it
 * when the page has no word for it. */
export function wordOf(
  t: (key: string) => string,
  prefix: string,
  code: string,
): string {
  const key = `${prefix}${code}`;
  return CATALOGUE.has(key) ? t(key) : code;
}
