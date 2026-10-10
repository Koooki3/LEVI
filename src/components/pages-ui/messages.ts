/**
 * Server sentences as the page's language shows them. The pages show what
 * the service says (why a request failed) next to what the page itself says
 * (what to do); a Chinese page must not end up with the English sentence
 * beside the Chinese one. Order of attempts:
 *
 * 1. a sentence the service wrote in both languages ("English / 中文"):
 *    the half for the current language;
 * 2. a known sentence with numbers or paths in it (a template below);
 * 3. a sentence the catalogue has (exact key, or the locale's own patterns);
 * 4. in Chinese, an English sentence nobody translated: a plain "no
 *    translation" line, the original under the technical details.
 *
 * Styles: none. Used by `feedback.tsx` and the pool and conversion pages.
 */
import { useLocale } from "@/components/levi-locale";

/** What a person reads: the reason, optionally a next step, and the
 * original wording when it could not be shown in the current language. */
export interface Described {
  text: string;
  fix?: string;
  details?: string;
}

type Translate = (text: string) => string;

const CJK = /[㐀-鿿]/;

/** "Error: busy" → "busy": the exception's class name says nothing to a
 * person (String(error) adds it). */
export function cleanMessage(message: string): string {
  return message.replace(/^(?:[A-Z]\w*)?Error:\s*/, "").trim();
}

/** A sentence written in both languages as "English / 中文" (the service's
 * non-blocking notes are): the half for `language`. */
export function pickLanguage(text: string, language: "en" | "zh"): string {
  const match = /^([\s\S]*?\S)\s\/\s(?=[\d\s]*[㐀-鿿])([\s\S]*)$/.exec(text);
  if (!match || CJK.test(match[1])) return text;
  return language === "zh" ? match[2].trim() : match[1].trim();
}

interface Known {
  re: RegExp;
  /** English catalogue key with {1}, {2} for the captured groups. */
  why: string;
  /** Fills the groups itself when they need translating too. */
  render?: (
    match: RegExpExecArray,
    t: Translate,
    language: "en" | "zh",
  ) => string;
  fix?: string;
}

const KNOWN: Known[] = [
  {
    // src/utils/backendProxy.ts: the page's own proxy could not reach the API.
    re: /^LEVI backend unavailable\./,
    why: "LEVI's service did not answer. It may be stopped or still starting.",
    fix: "Start both services with `uv run levi serve`, then try again.",
  },
  {
    // src/utils/backendProxy.ts: the page was opened under a host name the
    // bridge does not answer to (421), seen as the proxy's own sentence or
    // as a status a file read reports.
    re: /^(?:Request blocked: this LEVI page does not answer to the address|Cannot read the local dataset .+: the LEVI file service answered 421\.$|Failed to fetch JSON .+: 421\b|HTTP 421$)/,
    why: "Request blocked: LEVI was opened under an address that is not on its list of allowed host names.",
    fix: "Open LEVI as http://127.0.0.1:7860 (or localhost, on any port), or add this address to LEVI_UI_ALLOWED_HOSTS in .env and restart LEVI.",
  },
  {
    // src/utils/backendProxy.ts: a write that did not come from the page (403).
    re: /^Request blocked: writes through the web UI must come from the LEVI page itself/,
    why: "Request blocked: LEVI accepts changes only from its own page.",
    fix: "Reload this LEVI page and try again. Scripts make changes through the levi command line, not through the web page.",
  },
  {
    re: /^Path must remain inside the configured workspace/,
    why: "This folder is outside the LEVI workspace, the only place datasets can be registered from.",
    fix: "Enter a path inside the workspace shown on this page, or copy the dataset into it first.",
  },
  {
    re: /^Export directory (.+) is outside LEVI_EXPORT_ROOTS \((.*)\)$/,
    why: "The export folder {1} is outside the folders LEVI may export to ({2}).",
    fix: "Enter an output folder inside LEVI_EXPORT_ROOTS, or add this folder to LEVI_EXPORT_ROOTS and restart LEVI.",
  },
  {
    re: /^Export directory (.+) lies inside the source dataset (.+)$/,
    why: "The export folder {1} lies inside the source dataset {2}.",
    fix: "Choose an export folder outside every source dataset.",
  },
  {
    re: /^Export directory (.+) would contain the source dataset (.+)$/,
    why: "The export folder {1} would contain the source dataset {2}.",
    fix: "Choose an export folder outside every source dataset.",
  },
  {
    re: /^Export directory already exists: (.+)$/,
    why: "The export folder {1} already exists.",
    fix: "Choose another dataset name or folder, or delete the old one.",
  },
  {
    re: /^An unfinished export is in the way: (.+)$/,
    why: "An unfinished export is in the way: {1}",
    fix: "Resume that export from the job list, or delete it, then start again.",
  },
  {
    re: /^Refusing to write inside pool source (.+)$/,
    why: "LEVI refuses to write inside the pool source {1}; sources stay read-only.",
    fix: "Choose a folder outside every pool source.",
  },
  {
    re: /^The worker died from signal (\S+) \(exit (-?\d+)\)$/,
    why: "The worker was stopped by signal {1} (exit {2}).",
  },
  {
    re: /^The worker exited with code (-?\d+) without a result$/,
    why: "The worker exited with code {1} without writing a result.",
  },
  {
    re: /^(\d+) approved task correction\(s\) disagree with another one for the same recording; reject one of them or apply fewer versions$/,
    why: "{1} approved task correction(s) disagree with another one for the same recording. Reject one of them or apply fewer versions.",
  },
  {
    re: /^Approved task corrections not applied: (.*?) \(stale: .*\)$/,
    why: "Approved task corrections not applied: {1}. Stale: the episode's text is no longer the one corrected. Unmatched: no such episode in the index. Ambiguous: the path is found under more than one pool root.",
    render: (match, t) =>
      fill(
        t(
          "Approved task corrections not applied: {1}. Stale: the episode's text is no longer the one corrected. Unmatched: no such episode in the index. Ambiguous: the path is found under more than one pool root.",
        ),
        [match[0], counted(match[1], t)],
      ),
  },
  {
    // Conversion inspection (levi/conversion/inputs/robot_capture.py).
    re: /^Folder name used for: (.*?); (\d+) task\(s\)$/,
    why: "The task text comes from the folder name for: {1}; {2} task(s)",
    render: (match, t) =>
      fill(
        t("The task text comes from the folder name for: {1}; {2} task(s)"),
        [
          match[0],
          match[1].replace(/\(\+(\d+) more\)/, (_, n) =>
            t("(+{1} more)").replace("{1}", n),
          ),
          match[2],
        ],
      ),
  },
  {
    // Fast segmentation status (levi/segmentation/jobs.py, backend/segmentation.py).
    re: /^worker source is missing from this checkout$/,
    why: "The student worker's source is missing from this checkout.",
  },
  {
    re: /^worker environment not found at (.+?); run integrations\/segmentation\/setup\.sh$/,
    why: "The student worker environment was not found at {1}.",
  },
  {
    re: /^SAM3 worker environment not found at (.+)$/,
    why: "The SAM3 worker environment was not found at {1}.",
  },
  {
    re: /^SAM3 checkpoint is not downloaded$/,
    why: "The SAM3 checkpoint has not been downloaded.",
  },
  {
    re: /^The SAM3 teacher is not ready: (.+)$/,
    why: "The SAM3 teacher is not ready: {1}",
    render: (match, t, language) =>
      fill(t("The SAM3 teacher is not ready: {1}"), [
        match[0],
        serverSentence(match[1], t, language),
      ]),
  },
  {
    // levi/views.py: registering a folder that is not a dataset.
    re: /^Not a LeRobot dataset \(meta\/info\.json\) or a recognized raw capture$/,
    why: "This folder is not a LeRobot dataset (no meta/info.json) or a recognized raw capture.",
    fix: "Check the path: a LeRobot dataset has meta/info.json; a raw capture has task folders with demo_NNNN folders inside.",
  },
  {
    re: /^HTTP (\d+)$/,
    why: "The service answered with an error (HTTP {1}).",
  },
];

const UNTRANSLATED =
  "The service gave a reason that has no translation. Its own words are under Technical details.";

/** "1 stale, 2 unmatched" with the kinds translated: "1 条已过期，2 条未匹配"
 * (English keeps its own words and its comma). */
function counted(list: string, t: Translate): string {
  let translated = false;
  const parts = list.split(", ").map((part) => {
    const found = /^(\d+) (\w+)$/.exec(part);
    if (!found) return part;
    const word = t(found[2]);
    if (word === found[2]) return part;
    translated = true;
    return `${found[1]} 条${word}`;
  });
  return parts.join(translated ? "，" : ", ");
}

function fill(template: string, match: ArrayLike<string>): string {
  return template.replace(
    /\{(\d)\}/g,
    (_, index) => match[Number(index)] ?? "",
  );
}

export function describeMessage(
  raw: string,
  t: Translate,
  language: "en" | "zh",
): Described {
  const cleaned = pickLanguage(cleanMessage(raw), language);
  for (const known of KNOWN) {
    const match = known.re.exec(cleaned);
    if (!match) continue;
    return {
      text: known.render
        ? known.render(match, t, language)
        : fill(t(known.why), match),
      fix: known.fix ? t(known.fix) : undefined,
    };
  }
  const translated = t(cleaned);
  if (language === "en" || translated !== cleaned || CJK.test(cleaned))
    return { text: translated };
  // "Label: detail" (an inspection result): the label is in the catalogue
  // and the detail may be a sentence of its own.
  const joined = /^([^:]+): (.+)$/.exec(cleaned);
  if (joined && t(joined[1]) !== joined[1]) {
    const detail = describeMessage(joined[2], t, language);
    if (!detail.details) return { text: `${t(joined[1])}：${detail.text}` };
  }
  if (!/[A-Za-z]{3}/.test(cleaned)) return { text: cleaned };
  return { text: t(UNTRANSLATED), details: cleaned };
}

/** A server sentence for a line of text (a warning in a list): the current
 * language's wording, or the original when nothing could translate it. */
export function serverSentence(
  raw: string,
  t: Translate,
  language: "en" | "zh",
): string {
  const described = describeMessage(raw, t, language);
  return described.details ?? described.text;
}

/** `describe(raw)` for the current language. */
export function useDescribe(): (raw: string) => Described {
  const { t, language } = useLocale();
  return (raw) => describeMessage(raw, t, language);
}

/** A server sentence inline (a note under a job): the current language's
 * wording; the original when nothing could translate it, because a line of
 * text has no room for a "technical details" fold. */
export function useServerText(): (raw: string) => string {
  const { t, language } = useLocale();
  return (raw) => serverSentence(raw, t, language);
}

/** The colon between a label and its value: full-width in Chinese, a colon
 * and a space in English. */
export function useColon(): string {
  const { language } = useLocale();
  return language === "zh" ? "：" : ": ";
}
