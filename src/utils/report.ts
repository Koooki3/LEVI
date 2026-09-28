/**
 * Technical report (levi/report.py, docs/API.md#technical-report--技术报告):
 * the pure parts of the /report page — fenced-block specs, status lookups,
 * number and time formats, headings. No React here, so it is unit-tested.
 */

export type ReportLang = "en" | "zh";
export type Bilingual =
  | string
  | { en?: string | null; zh?: string | null }
  | null
  | undefined;

export type WorkstreamState =
  | "running"
  | "done"
  | "blocked"
  | "waiting"
  | "planned";

export interface Workstream {
  id: string;
  title?: Bilingual;
  state?: string;
  progress?: number | null;
  stage?: Bilingual;
  started_at?: string | null;
  updated_at?: string | null;
  eta?: string | null;
  owner?: Bilingual;
  links?: { label?: Bilingual; path?: string }[];
}

export type MetricFormat = "0.000" | "pct" | "int" | "seconds" | string;

export interface Metric {
  label?: Bilingual;
  value?: number | null;
  format?: MetricFormat;
  baseline?: number | null;
  better?: "higher" | "lower" | string;
  note?: Bilingual;
}

export interface ReportTable {
  columns: { key: string; label?: Bilingual }[];
  rows: Record<string, unknown>[];
}

export interface Milestone {
  date?: string;
  title?: Bilingual;
  state?: "done" | "current" | "next" | string;
}

export interface ReportResources {
  gpu?: {
    used_mib?: number | null;
    total_mib?: number | null;
    holder?: string | null;
  } | null;
  disk_free_gb?: number | null;
}

export interface ReportStatus {
  schema?: string;
  generated_at?: string;
  levi_main?: string;
  workstreams?: Workstream[];
  metrics?: Record<string, Metric[]>;
  charts?: Record<string, Record<string, unknown>[]>;
  tables?: Record<string, ReportTable>;
  milestones?: Record<string, Milestone[]>;
  resources?: ReportResources;
}

export interface ReportPayload {
  configured: boolean;
  dir: string | null;
  exists: boolean;
  lang: ReportLang;
  document_lang: ReportLang | null;
  markdown: string | null;
  status: ReportStatus | null;
  errors: string[];
  mtime: number | null;
  etag: string;
}

/** A bilingual field in the current language, English when missing. */
export function pick(value: Bilingual, lang: ReportLang): string {
  if (value == null) return "";
  if (typeof value === "string") return value;
  return value[lang] || value.en || value.zh || "";
}

// ------------------------------------------------------------ fenced blocks

export const REPORT_BLOCKS = [
  "levi-progress",
  "levi-chart",
  "levi-metrics",
  "levi-table",
  "levi-timeline",
] as const;
export type ReportBlockLanguage = (typeof REPORT_BLOCKS)[number];

export interface ChartSeries {
  key: string;
  label?: Bilingual;
}

export type BlockSpec =
  | { kind: "progress"; source?: "workstreams"; id?: string }
  | {
      kind: "chart";
      type: "bar" | "line" | "grouped-bar";
      title?: Bilingual;
      data: string | Record<string, unknown>[];
      x: string;
      series: ChartSeries[];
      y_label?: Bilingual;
      y_domain?: [number, number];
    }
  | { kind: "metrics"; data: string | Metric[] }
  | { kind: "table"; data: string | ReportTable }
  | { kind: "timeline"; data: string | Milestone[] };

export type ParsedBlock =
  | { ok: true; spec: BlockSpec }
  | { ok: false; error: string };

export function isReportBlock(
  language: string | undefined | null,
): language is ReportBlockLanguage {
  return (REPORT_BLOCKS as readonly string[]).includes(language ?? "");
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function dataRef(
  body: Record<string, unknown>,
  inline: (value: unknown) => boolean,
): string | undefined | null {
  const data = body.data;
  if (typeof data === "string" && data.trim()) return data.trim();
  if (inline(data)) return null; // inline data, accepted by the caller
  return undefined;
}

/** Parse one fenced block's JSON body; a readable error, never a throw. */
export function parseBlock(
  language: ReportBlockLanguage,
  source: string,
): ParsedBlock {
  let body: unknown;
  try {
    body = JSON.parse(source);
  } catch (error) {
    return {
      ok: false,
      error: `Invalid JSON: ${error instanceof Error ? error.message : String(error)}`,
    };
  }
  if (!isObject(body))
    return { ok: false, error: "The block must be a JSON object" };
  switch (language) {
    case "levi-progress": {
      if (typeof body.id === "string" && body.id)
        return { ok: true, spec: { kind: "progress", id: body.id } };
      if (body.source === "workstreams" || body.source === undefined)
        return {
          ok: true,
          spec: { kind: "progress", source: "workstreams" },
        };
      return {
        ok: false,
        error: 'Use {"source": "workstreams"} or {"id": "<workstream>"}',
      };
    }
    case "levi-chart": {
      const type = body.type ?? "bar";
      if (type !== "bar" && type !== "line" && type !== "grouped-bar")
        return {
          ok: false,
          error: `Unknown chart type ${JSON.stringify(type)}: use bar, line or grouped-bar`,
        };
      const ref = dataRef(
        body,
        (d) => Array.isArray(d) && d.every((row) => isObject(row)),
      );
      if (ref === undefined)
        return {
          ok: false,
          error: '"data" must name a key of status.json charts or be rows',
        };
      const x = typeof body.x === "string" && body.x ? body.x : "x";
      let series: ChartSeries[] = [];
      if (body.series !== undefined) {
        if (
          !Array.isArray(body.series) ||
          !body.series.every(
            (item) => isObject(item) && typeof item.key === "string",
          )
        )
          return {
            ok: false,
            error: '"series" must be a list of {"key", "label"}',
          };
        series = body.series as ChartSeries[];
      }
      const domain = body.y_domain;
      const yDomain =
        Array.isArray(domain) &&
        domain.length === 2 &&
        domain.every((v) => typeof v === "number" && Number.isFinite(v))
          ? (domain as [number, number])
          : undefined;
      return {
        ok: true,
        spec: {
          kind: "chart",
          type,
          title: body.title as Bilingual,
          data: ref ?? (body.data as Record<string, unknown>[]),
          x,
          series,
          y_label: body.y_label as Bilingual,
          y_domain: yDomain,
        },
      };
    }
    case "levi-metrics": {
      const ref = dataRef(body, Array.isArray);
      if (ref === undefined)
        return { ok: false, error: '"data" must name a key of metrics' };
      return {
        ok: true,
        spec: { kind: "metrics", data: ref ?? (body.data as Metric[]) },
      };
    }
    case "levi-table": {
      const ref = dataRef(
        body,
        (d) => isObject(d) && Array.isArray(d.columns) && Array.isArray(d.rows),
      );
      if (ref === undefined)
        return { ok: false, error: '"data" must name a key of tables' };
      return {
        ok: true,
        spec: { kind: "table", data: ref ?? (body.data as ReportTable) },
      };
    }
    case "levi-timeline": {
      const ref = dataRef(body, Array.isArray);
      if (ref === undefined)
        return { ok: false, error: '"data" must name a key of milestones' };
      return {
        ok: true,
        spec: { kind: "timeline", data: ref ?? (body.data as Milestone[]) },
      };
    }
  }
}

// ------------------------------------------------------------ status lookups

export type Lookup<T> = { ok: true; value: T } | { ok: false; error: string };

function missing(section: string, key: string): { ok: false; error: string } {
  return { ok: false, error: `status.json has no ${section} "${key}"` };
}

export function lookupWorkstreams(
  status: ReportStatus | null,
  spec: { id?: string },
): Lookup<Workstream[]> {
  const all = Array.isArray(status?.workstreams) ? status.workstreams : [];
  if (!spec.id) return { ok: true, value: all };
  const found = all.filter((item) => item?.id === spec.id);
  return found.length
    ? { ok: true, value: found }
    : missing("workstream", spec.id);
}

/**
 * What people see for a workstream (or a GPU holder / owner that names one):
 * its title in `lang`; the id only when there is no title. `id` goes to the
 * tooltip — internal ids are for the ledger, not the page.
 */
export function workstreamLabel(
  status: ReportStatus | null | undefined,
  id: string | null | undefined,
  lang: ReportLang,
): { text: string; id: string | null } {
  if (!id) return { text: "", id: null };
  const all = Array.isArray(status?.workstreams) ? status.workstreams : [];
  const found = all.find((item) => item?.id === id);
  const title = found ? pick(found.title, lang) : "";
  return title ? { text: title, id } : { text: id, id: null };
}

function lookupKey<T>(
  status: ReportStatus | null,
  section: "metrics" | "charts" | "tables" | "milestones",
  data: string | T,
  valid: (value: unknown) => boolean,
): Lookup<T> {
  if (typeof data !== "string") return { ok: true, value: data };
  const store = status?.[section] as Record<string, unknown> | undefined;
  const value = store && isObject(store) ? store[data] : undefined;
  if (value === undefined) return missing(section, data);
  if (!valid(value))
    return {
      ok: false,
      error: `status.json ${section} "${data}" is malformed`,
    };
  return { ok: true, value: value as T };
}

export const lookupMetrics = (
  status: ReportStatus | null,
  data: string | Metric[],
) => lookupKey<Metric[]>(status, "metrics", data, Array.isArray);
export const lookupChart = (
  status: ReportStatus | null,
  data: string | Record<string, unknown>[],
) =>
  lookupKey<Record<string, unknown>[]>(status, "charts", data, Array.isArray);
export const lookupTable = (
  status: ReportStatus | null,
  data: string | ReportTable,
) =>
  lookupKey<ReportTable>(
    status,
    "tables",
    data,
    (d) => isObject(d) && Array.isArray(d.columns) && Array.isArray(d.rows),
  );
export const lookupMilestones = (
  status: ReportStatus | null,
  data: string | Milestone[],
) => lookupKey<Milestone[]>(status, "milestones", data, Array.isArray);

/** Series to draw: the declared ones, else every numeric column but x. */
export function chartSeries(
  rows: Record<string, unknown>[],
  x: string,
  declared: ChartSeries[],
): ChartSeries[] {
  if (declared.length) return declared;
  const keys: string[] = [];
  for (const row of rows)
    for (const [key, value] of Object.entries(row))
      if (key !== x && typeof value === "number" && !keys.includes(key))
        keys.push(key);
  return keys.map((key) => ({ key }));
}

// ------------------------------------------------------------ formats

function finite(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function decimalsOf(format: string): number | null {
  const match = /^0(?:\.(0+))?$/.exec(format);
  return match ? (match[1]?.length ?? 0) : null;
}

/** Seconds as "42 s", "3 min 05 s", "1 h 05 min" (zh: 秒 / 分 / 小时). */
export function formatDuration(seconds: number, lang: ReportLang): string {
  const sign = seconds < 0 ? "−" : "";
  let s = Math.abs(seconds);
  const u =
    lang === "zh"
      ? { s: " 秒", m: " 分", h: " 小时" }
      : { s: " s", m: " min", h: " h" };
  if (s < 10) return `${sign}${Number(s.toFixed(1))}${u.s}`;
  s = Math.round(s);
  if (s < 60) return `${sign}${s}${u.s}`;
  if (s < 3600) {
    const m = Math.floor(s / 60);
    const rest = s % 60;
    return `${sign}${m}${u.m}${rest ? ` ${String(rest).padStart(2, "0")}${u.s}` : ""}`;
  }
  const h = Math.floor(s / 3600);
  const m = Math.round((s % 3600) / 60);
  return `${sign}${h}${u.h}${m ? ` ${String(m).padStart(2, "0")}${u.m}` : ""}`;
}

export function formatNumber(
  value: unknown,
  format: MetricFormat | undefined,
  lang: ReportLang = "en",
): string {
  if (!finite(value)) return value == null ? "—" : String(value);
  if (format === "pct") return `${(value * 100).toFixed(1)}%`;
  if (format === "int") return Math.round(value).toLocaleString("en-US");
  if (format === "seconds") return formatDuration(value, lang);
  const decimals = format ? decimalsOf(format) : null;
  if (decimals !== null) return value.toFixed(decimals);
  if (Number.isInteger(value)) return value.toLocaleString("en-US");
  return String(Number(value.toPrecision(4)));
}

export type Tone = "good" | "bad" | "neutral";

export function deltaTone(
  value: unknown,
  baseline: unknown,
  better: string | undefined,
): Tone {
  if (!finite(value) || !finite(baseline) || value === baseline)
    return "neutral";
  if (better !== "higher" && better !== "lower") return "neutral";
  return value > baseline === (better === "higher") ? "good" : "bad";
}

/** The change from the baseline: "+12.8 pp", "−0.034", "−3 min 10 s (−35%)". */
export function formatDelta(
  value: unknown,
  baseline: unknown,
  format: MetricFormat | undefined,
  lang: ReportLang = "en",
): string | null {
  if (!finite(value) || !finite(baseline)) return null;
  const diff = value - baseline;
  const sign = diff > 0 ? "+" : diff < 0 ? "−" : "±";
  const magnitude = Math.abs(diff);
  if (format === "pct") return `${sign}${(magnitude * 100).toFixed(1)} pp`;
  const body =
    format === "seconds"
      ? formatDuration(magnitude, lang)
      : formatNumber(magnitude, format, lang);
  if (format === "seconds" && baseline !== 0)
    return `${sign}${body} (${sign}${Math.round((magnitude / Math.abs(baseline)) * 100)}%)`;
  return `${sign}${body}`;
}

export function clampProgress(value: unknown): number | null {
  if (!finite(value)) return null;
  return Math.min(1, Math.max(0, value));
}

const STATES: WorkstreamState[] = [
  "running",
  "done",
  "blocked",
  "waiting",
  "planned",
];

export function normalizeState(state: unknown): WorkstreamState {
  return STATES.includes(state as WorkstreamState)
    ? (state as WorkstreamState)
    : "planned";
}

/** Order for display: running first, then blocked, waiting, planned, done. */
export function sortWorkstreams(items: Workstream[]): Workstream[] {
  const rank: Record<WorkstreamState, number> = {
    running: 0,
    blocked: 1,
    waiting: 2,
    planned: 3,
    done: 4,
  };
  return items
    .map((item, index) => ({ item, index }))
    .sort(
      (a, b) =>
        rank[normalizeState(a.item.state)] -
          rank[normalizeState(b.item.state)] || a.index - b.index,
    )
    .map(({ item }) => item);
}

function parseTime(value: string | null | undefined): Date | null {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** "09-28 21:04" in the viewer's local time; the input as-is if unparseable. */
export function formatStamp(value: string | null | undefined): string {
  const date = parseTime(value);
  if (!date) return value ?? "—";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** "3 min ago" / "3 分钟前"; "just now" under a minute. */
export function formatAge(
  value: string | null | undefined,
  now: number,
  lang: ReportLang,
): string | null {
  const date = parseTime(value);
  if (!date) return null;
  const seconds = Math.max(0, (now - date.getTime()) / 1000);
  if (seconds < 60) return lang === "zh" ? "刚刚" : "just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60)
    return lang === "zh" ? `${minutes} 分钟前` : `${minutes} min ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return lang === "zh" ? `${hours} 小时前` : `${hours} h ago`;
  const days = Math.floor(hours / 24);
  return lang === "zh" ? `${days} 天前` : `${days} d ago`;
}

/** An ETA as a time plus what remains ("22:30 · in 1 h 05 min"), or verbatim. */
export function formatEta(
  value: string | null | undefined,
  now: number,
  lang: ReportLang,
): string | null {
  if (!value) return null;
  const date = parseTime(value);
  if (!date) return value;
  const remaining = (date.getTime() - now) / 1000;
  const stamp = formatStamp(value);
  if (remaining <= 0)
    return lang === "zh" ? `${stamp} · 已过` : `${stamp} · overdue`;
  const left = formatDuration(remaining, lang);
  return lang === "zh" ? `${stamp} · 约 ${left}后` : `${stamp} · in ${left}`;
}

/** "11.2 / 31.8 GiB" from MiB; null when unknown. */
export function formatGpu(resources: ReportResources | undefined | null) {
  const gpu = resources?.gpu;
  if (!gpu || !finite(gpu.total_mib) || gpu.total_mib <= 0) return null;
  const used = finite(gpu.used_mib) ? gpu.used_mib : 0;
  return {
    text: `${(used / 1024).toFixed(1)} / ${(gpu.total_mib / 1024).toFixed(1)} GiB`,
    fraction: Math.min(1, Math.max(0, used / gpu.total_mib)),
    holder: gpu.holder || null,
  };
}

// ------------------------------------------------------------ tables

export type SortDirection = "asc" | "desc";

/** A cell's sortable value: numbers stay numbers, bilingual text is picked. */
function sortValue(value: unknown, lang: ReportLang): unknown {
  if (value !== null && typeof value === "object" && !Array.isArray(value))
    return pick(value as Bilingual, lang);
  return value;
}

/** Numbers numerically, text naturally; empty cells always last. */
export function sortRows(
  rows: Record<string, unknown>[],
  key: string | null,
  direction: SortDirection,
  lang: ReportLang = "en",
): Record<string, unknown>[] {
  if (!key) return rows;
  const factor = direction === "asc" ? 1 : -1;
  const empty = (v: unknown) => v === null || v === undefined || v === "";
  return rows
    .map((row, index) => ({ row, index, value: sortValue(row[key], lang) }))
    .sort((a, b) => {
      const x = a.value;
      const y = b.value;
      if (empty(x) || empty(y))
        return empty(x) === empty(y) ? a.index - b.index : empty(x) ? 1 : -1;
      const order =
        finite(x) && finite(y)
          ? x - y
          : String(x).localeCompare(String(y), lang === "zh" ? "zh" : "en", {
              numeric: true,
            });
      return order * factor || a.index - b.index;
    })
    .map(({ row }) => row);
}

export function cellText(value: unknown, lang: ReportLang): string {
  if (value == null) return "";
  if (typeof value === "number") return formatNumber(value, undefined, lang);
  if (typeof value === "object" && !Array.isArray(value))
    return pick(value as Bilingual, lang);
  return String(value);
}

// ------------------------------------------------------------ headings

export interface Heading {
  level: number;
  text: string;
  id: string;
  line: number;
}

/** Plain text of a heading's inline Markdown. */
export function headingText(source: string): string {
  return source
    .replace(/\s+#+\s*$/, "")
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/[`*_~]/g, "")
    .trim();
}

/** A URL fragment that keeps CJK text readable. */
export function slugify(text: string): string {
  const slug = text
    .toLowerCase()
    .normalize("NFKC")
    .replace(/[^\p{L}\p{N}\s-]/gu, "")
    .trim()
    .replace(/\s+/g, "-");
  return slug || "section";
}

/**
 * ATX headings (# … ######) outside fenced code, with unique ids; `line` is
 * 1-based, matching the Markdown parser's positions.
 */
export function extractHeadings(markdown: string): Heading[] {
  const headings: Heading[] = [];
  const seen = new Map<string, number>();
  let fence: string | null = null;
  markdown.split(/\r?\n/).forEach((raw, index) => {
    const open = /^ {0,3}(`{3,}|~{3,})/.exec(raw);
    if (fence) {
      if (
        open &&
        open[1][0] === fence[0] &&
        open[1].length >= fence.length &&
        !raw.trim().slice(open[1].length).trim()
      )
        fence = null;
      return;
    }
    if (open) {
      fence = open[1];
      return;
    }
    const match = /^ {0,3}(#{1,6})\s+(.+)$/.exec(raw);
    if (!match) return;
    const text = headingText(match[2]);
    if (!text) return;
    const base = slugify(text);
    const count = seen.get(base) ?? 0;
    seen.set(base, count + 1);
    headings.push({
      level: match[1].length,
      text,
      id: count ? `${base}-${count + 1}` : base,
      line: index + 1,
    });
  });
  return headings;
}

// ------------------------------------------------------------ assets

export const REPORT_ASSET_ROUTE = "/api/levi/report/assets/";

/**
 * Where an image in the report is loaded from: `assets/<file>` goes through
 * the report asset route; http(s) and data URLs pass; anything else is null
 * (not servable — the page shows the alt text).
 */
export function assetSrc(
  src: string | undefined | null,
  version = "",
): string | null {
  if (!src) return null;
  if (/^(https?:|data:image\/)/i.test(src)) return src;
  const path = src.replace(/^\.\//, "");
  if (!path.startsWith("assets/")) return null;
  const rest = path.slice("assets/".length).split(/[?#]/)[0];
  const parts = rest.split("/");
  if (!rest || parts.some((part) => !part || part === "." || part === ".."))
    return null;
  const query = version ? `?v=${encodeURIComponent(version)}` : "";
  return REPORT_ASSET_ROUTE + parts.map(encodeURIComponent).join("/") + query;
}

/** A link the page can follow: web links, mail and in-page anchors. */
export function isFollowableLink(href: string | undefined | null): boolean {
  return !!href && /^(https?:|mailto:|#)/i.test(href);
}
