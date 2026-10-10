// Which of the report's files the page draws, links or reads as text. The
// report endpoint lists `files: [{name, kind}]` (levi/automatic/campaign/
// report.py: figures/<id>.json|svg|pdf, tables/*.csv|tex, data/*, summary.*.md,
// manifest.json); the page sorts them by their path, not by trusting `kind`.
import type { ReportFile } from "./wizard-types";

const FIGURE_JSON = /(^|\/)figures\/[^/]+\.json$/;

/** The figure specs to fetch and draw, in name order (f1…f7). */
export function figureFiles(files: ReportFile[]): ReportFile[] {
  return files
    .filter((f) => FIGURE_JSON.test(f.name))
    .sort((a, b) => a.name.localeCompare(b.name, "en"));
}

/** The summary text file for the page language (Chinese falls back to English). */
export function summaryFile(
  files: ReportFile[],
  language: "en" | "zh",
): ReportFile | null {
  const names =
    language === "zh"
      ? ["summary.zh-CN.md", "summary.en.md"]
      : ["summary.en.md", "summary.zh-CN.md"];
  for (const name of names) {
    const found = files.find(
      (f) => f.name === name || f.name.endsWith(`/${name}`),
    );
    if (found) return found;
  }
  return null;
}

export type DownloadGroup = { key: string; files: ReportFile[] };

/** Files to offer as downloads, grouped: tables, figures, data, the rest. */
export function downloadGroups(files: ReportFile[]): DownloadGroup[] {
  const groups: Record<string, ReportFile[]> = {
    tables: [],
    figures: [],
    data: [],
    other: [],
  };
  for (const file of files) {
    if (/(^|\/)summary\.[^/]+\.md$/.test(file.name)) continue;
    if (/(^|\/)tables\//.test(file.name)) groups.tables.push(file);
    else if (/(^|\/)figures\//.test(file.name)) groups.figures.push(file);
    else if (/(^|\/)data\//.test(file.name)) groups.data.push(file);
    else groups.other.push(file);
  }
  return Object.entries(groups)
    .filter(([, list]) => list.length > 0)
    .map(([key, list]) => ({
      key,
      files: list.sort((a, b) => a.name.localeCompare(b.name, "en")),
    }));
}

/** The basis in the address, if it is one the page knows (else the default). */
export function basisFromQuery(
  value: string | null | undefined,
  known: string[],
  fallback: string,
): string {
  return value && known.includes(value) ? value : fallback;
}
