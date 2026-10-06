// Pure parts of the live statistics panel: the shapes of
// `GET /api/levi/live/stats` (docs/LIVE.md, "Statistics and reports"), how a
// scope becomes a URL, the key figures and how a number is written.
// Every field is optional on purpose: an older service, or a figure that was
// not measured, arrives as null or not at all and shows as "—".
import type { Agreement, AgreementSummary } from "./types";

export interface Dist {
  n?: number;
  min?: number | null;
  median?: number | null;
  p90?: number | null;
  max?: number | null;
  mean?: number | null;
}

export interface StatsSummary {
  episodes?: {
    count?: number;
    records?: number;
    done?: number;
    failed?: number;
    retrying?: number;
    retried?: number;
    excluded?: number;
  };
  latency?: Record<string, Dist>;
  throughput?: {
    episode_seconds?: number | null;
    model_seconds?: number | null;
    realtime_factor?: number | null;
    span_s?: number | null;
    wall_factor?: number | null;
  };
  model?: {
    tokens_total?: number | null;
    tokens_per_episode?: Dist;
    prompt_share?: number | null;
    requests_per_episode?: Dist;
    images_per_episode?: Dist;
    external_tokens?: number | null;
    by_kind?: Record<
      string,
      {
        requests?: number | null;
        seconds?: number | null;
        seconds_share?: number | null;
        tokens?: number | null;
        tokens_share?: number | null;
      }
    >;
  };
  gpu?: {
    batches?: number;
    closed_wait_s?: number | null;
    interruptions?: number | null;
    vllm_wakes?: {
      count?: number;
      total_s?: number | null;
      max_s?: number | null;
    };
    vllm_cold_starts?: { count?: number; total_s?: number | null };
    vllm_sleeps?: number | null;
    gate_window?: {
      closed_s?: number;
      closures?: number;
      closed_share?: number;
    } | null;
  };
  outcome?: {
    segments_total?: number | null;
    labels?: Record<string, number>;
    verdicts?: Record<string, number>;
    review?: Record<string, number>;
  };
  in_session?: {
    count?: number;
    evaluable?: number;
    ratio?: number | null;
  };
  /** The agent label against the operator label (ground truth). */
  agreement?: AgreementSummary;
}

export interface StatsSession {
  dataset?: string | null;
  session?: string | null;
  episodes?: number;
  done?: number;
  failed?: number;
  first_at?: number | null;
  last_at?: number | null;
  to_commit_median_s?: number | null;
  to_verdict_median_s?: number | null;
  to_verdict_p90_s?: number | null;
  tokens?: number | null;
  model_seconds?: number | null;
  realtime_factor?: number | null;
  in_session_ratio?: number | null;
  closed_wait_s?: number | null;
  success?: number;
  failure?: number;
  /** Agent vs operator: episodes with an operator success/failure... */
  pairs?: number;
  /** ...where the agent agrees, of those it judged success/failure. */
  agree?: number;
  judged?: number;
  false_success?: number;
  missed_success?: number;
  operator_success?: number;
}

export interface StatsEpisode {
  dataset?: string | null;
  demo?: string | null;
  episode_index?: number | null;
  session?: string | null;
  at?: number | null;
  state?: string | null;
  attempts?: number | null;
  episode_seconds?: number | null;
  to_mirror_s?: number | null;
  to_first_request_s?: number | null;
  to_commit_s?: number | null;
  to_verdict_s?: number | null;
  requests?: number | null;
  model_seconds?: number | null;
  total_tokens?: number | null;
  images?: number | null;
  closed_wait_s?: number | null;
  segments?: number | null;
  verdict?: string | null;
  review?: string | null;
  in_session?: boolean | null;
  /** The episode was removed by a person (shown only when included). */
  excluded?: boolean;
  /** The operator label (ground truth): success, failure, unlabeled... */
  operator?: string | null;
  agreement?: Agreement | null;
}

export interface StatsResponse {
  enabled?: boolean;
  schema?: string;
  generated_at?: number;
  scope?: { dataset?: string | null; session?: string | null };
  include_excluded?: boolean;
  /** Removed episodes in this scope (left out unless included). */
  excluded_demos?: number;
  datasets?: string[];
  summary?: StatsSummary;
  sessions?: StatsSession[];
  sessions_total?: number;
  episodes?: {
    total?: number;
    offset?: number;
    limit?: number | null;
    rows?: StatsEpisode[];
  };
}

export interface StatsScope {
  dataset: string;
  session: string;
  /** Count the episodes a person removed too (off by default). */
  includeExcluded?: boolean;
}

export const EPISODE_PAGE = 25;
export const EXPORT_FORMATS = ["md", "json", "csv"] as const;
export type ExportFormat = (typeof EXPORT_FORMATS)[number];

function query(scope: StatsScope, extra: Record<string, string>): string {
  const params = new URLSearchParams();
  if (scope.dataset) params.set("dataset", scope.dataset);
  if (scope.session) params.set("session", scope.session);
  if (scope.includeExcluded) params.set("include_excluded", "true");
  for (const [key, value] of Object.entries(extra)) params.set(key, value);
  const text = params.toString();
  return text ? `?${text}` : "";
}

/** The request for the panel's data (`limit` episode rows, newest first). */
export function statsPath(scope: StatsScope, limit: number): string {
  return `stats${query(scope, { limit: String(limit) })}`;
}

/** The download link of one export format for a scope, in a language. */
export function exportHref(
  format: ExportFormat,
  scope: StatsScope,
  language: string,
): string {
  return `/api/levi/live/stats/export${query(scope, {
    format,
    lang: language === "zh" ? "zh" : "en",
  })}`;
}

/** Whether the panel should ask again: at most every 5 s while an evaluation
 * runs, every 30 s otherwise (a labelled episode moves the figures only when
 * a batch ends), and at once when nothing has been loaded. */
export function dueForRefresh(
  now: number,
  last: number | null,
  evaluating: boolean,
): boolean {
  if (last == null) return true;
  return now - last >= (evaluating ? 5000 : 30000);
}

const isNumber = (value: unknown): value is number =>
  typeof value === "number" && Number.isFinite(value);

/** A number of seconds with a unit: "4.3 s", "2 min 05 s"; "—" when unknown. */
export function seconds(value: number | null | undefined): string {
  if (!isNumber(value)) return "—";
  if (value < 100) return `${Number(value.toFixed(1))} s`;
  const total = Math.round(value);
  const m = Math.floor(total / 60);
  return m >= 60
    ? `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")} min`
    : `${m} min ${String(total % 60).padStart(2, "0")} s`;
}

/** A count with thousands separators; "—" when unknown. */
export function count(value: number | null | undefined): string {
  return isNumber(value) ? Math.round(value).toLocaleString("en-US") : "—";
}

export function percent(value: number | null | undefined): string {
  return isNumber(value) ? `${Math.round(value * 100)}%` : "—";
}

/** The real-time factor: "0.86×". */
export function factor(value: number | null | undefined): string {
  return isNumber(value) ? `${Number(value.toFixed(2))}×` : "—";
}

export interface Kpi {
  id: string;
  /** Catalog keys; `hint` may hold `{a}` / `{b}` placeholders. */
  label: string;
  value: string;
  hint: string;
  hintArgs: Record<string, string>;
}

/** The headline figures of a scope, in the order the page shows them. */
export function kpis(summary: StatsSummary | undefined): Kpi[] {
  const s = summary ?? {};
  const ep = s.episodes ?? {};
  const lat = s.latency ?? {};
  const verdict = lat.to_verdict_s ?? {};
  const model = s.model ?? {};
  const gpu = s.gpu ?? {};
  const ins = s.in_session ?? {};
  return [
    {
      id: "episodes",
      label: "Episodes",
      value: count(ep.count),
      hint: "{a} labelled, {b} failed",
      hintArgs: { a: count(ep.done), b: count(ep.failed) },
    },
    {
      id: "median",
      label: "Median delay",
      value: seconds(verdict.median),
      hint: "to the success/failure verdict; time segments after {a}",
      hintArgs: { a: seconds(lat.to_commit_s?.median) },
    },
    {
      id: "p90",
      label: "Slowest 10% (p90)",
      value: seconds(verdict.p90),
      hint: "longest {a}",
      hintArgs: { a: seconds(verdict.max) },
    },
    {
      id: "realtime",
      label: "Real-time factor",
      value: factor(s.throughput?.realtime_factor),
      hint: "episode seconds per model second",
      hintArgs: {},
    },
    {
      id: "tokens",
      label: "Tokens per episode",
      value: count(model.tokens_per_episode?.mean),
      hint: "{a} in total",
      hintArgs: { a: count(model.tokens_total) },
    },
    {
      id: "gate",
      label: "Waited for the GPU gate",
      value: seconds(gpu.closed_wait_s),
      hint: "{a} interruptions",
      hintArgs: { a: count(gpu.interruptions) },
    },
    {
      id: "insession",
      label: "Labelled during the session",
      value: percent(ins.ratio),
      hint: "{a} of {b} episodes",
      hintArgs: { a: count(ins.count), b: count(ins.evaluable) },
    },
  ];
}

/** Fill `{a}`-style placeholders in an already translated sentence. */
export function fill(text: string, args: Record<string, string>): string {
  return text.replace(/\{(\w+)\}/g, (_, key: string) => args[key] ?? "—");
}

export type EpisodeView = "latency" | "cost";

export interface Column {
  key: string;
  label: string;
  numeric: boolean;
  cell: (row: StatsEpisode) => string;
}

const text = (value: string | null | undefined) => value || "—";

/** The catalog key of one episode's agreement. */
export const AGREEMENT_LABEL: Record<Agreement, string> = {
  yes: "agrees",
  no: "disagrees",
  undecided: "agent undecided",
  no_agent: "no agent verdict yet",
};

/** Whether any episode row carries an operator success or failure (an
 * unattended episode's "unlabeled" is none): the agent-vs-operator columns
 * are shown only then. */
export function hasOperatorLabels(rows: StatsEpisode[] | undefined): boolean {
  return (rows ?? []).some(
    (r) => r.operator === "success" || r.operator === "failure",
  );
}

/** Whether any session has an episode the operator labelled success or failure. */
export function hasPairs(rows: StatsSession[] | undefined): boolean {
  return (rows ?? []).some((r) => (r.pairs ?? 0) > 0);
}

const OPERATOR_COLUMNS: Column[] = [
  {
    key: "operator",
    label: "Operator",
    numeric: false,
    cell: (r) => text(r.operator),
  },
  {
    key: "agreement",
    label: "Agree",
    numeric: false,
    cell: (r) => (r.agreement ? AGREEMENT_LABEL[r.agreement] : "—"),
  },
];

/** The per-episode table's columns for a view; the operator label and the
 * agreement are added only when some row has an operator label. */
export function columns(
  view: EpisodeView,
  rows: StatsEpisode[] = [],
): Column[] {
  const extra = hasOperatorLabels(rows) ? OPERATOR_COLUMNS : [];
  return [...viewColumns(view), ...extra];
}

function viewColumns(view: EpisodeView): Column[] {
  const demo: Column = {
    key: "demo",
    label: "Episode",
    numeric: false,
    cell: (r) => text(r.demo),
  };
  if (view === "latency")
    return [
      demo,
      {
        key: "to_mirror_s",
        label: "Mirrored",
        numeric: true,
        cell: (r) => seconds(r.to_mirror_s),
      },
      {
        key: "to_first_request_s",
        label: "First request",
        numeric: true,
        cell: (r) => seconds(r.to_first_request_s),
      },
      {
        key: "to_commit_s",
        label: "Time segments",
        numeric: true,
        cell: (r) => seconds(r.to_commit_s),
      },
      {
        key: "to_verdict_s",
        label: "Verdict",
        numeric: true,
        cell: (r) => seconds(r.to_verdict_s),
      },
      {
        key: "closed_wait_s",
        label: "Gate wait",
        numeric: true,
        cell: (r) => seconds(r.closed_wait_s),
      },
      {
        key: "in_session",
        label: "During session",
        numeric: false,
        cell: (r) => (r.in_session == null ? "—" : r.in_session ? "Yes" : "No"),
      },
    ];
  return [
    demo,
    {
      key: "episode_seconds",
      label: "Episode length",
      numeric: true,
      cell: (r) => seconds(r.episode_seconds),
    },
    {
      key: "requests",
      label: "Requests",
      numeric: true,
      cell: (r) => count(r.requests),
    },
    {
      key: "model_seconds",
      label: "Model time",
      numeric: true,
      cell: (r) => seconds(r.model_seconds),
    },
    {
      key: "total_tokens",
      label: "Tokens",
      numeric: true,
      cell: (r) => count(r.total_tokens),
    },
    {
      key: "images",
      label: "Images",
      numeric: true,
      cell: (r) => count(r.images),
    },
    {
      key: "segments",
      label: "Time segments",
      numeric: true,
      cell: (r) => count(r.segments),
    },
    {
      key: "verdict",
      label: "Automatic verdict",
      numeric: false,
      cell: (r) => text(r.verdict),
    },
  ];
}

/** Session ids to offer in the scope selector: those of the unfiltered answer
 * (of one dataset when one is chosen), newest first, plus the chosen one. */
export function sessionChoices(
  sessions: StatsSession[],
  dataset: string,
  chosen: string,
): string[] {
  const ids = sessions
    .filter((s) => s.session && (!dataset || s.dataset === dataset))
    .map((s) => s.session as string);
  if (chosen && !ids.includes(chosen)) ids.push(chosen);
  return [...new Set(ids)];
}

export interface StatsLoader {
  /** Ask for a scope; a newer ask abandons the one before it. Resolves with
   * the answer, or null when it was abandoned. Rejects on a failed request. */
  load: (scope: StatsScope, limit: number) => Promise<StatsResponse | null>;
  /** Abandon whatever is in flight. */
  stop: () => void;
  /** The path of the last ask (what the panel is showing the answer to). */
  last: () => string | null;
}

/** The request side of the panel, without React: it always asks for the
 * scope it is given (every part of it, the removed-episodes switch included)
 * and drops the answer to an ask that a newer one replaced. */
export function createStatsLoader(
  fetcher: (
    path: string,
    init: { cache: "no-store"; signal: AbortSignal },
  ) => Promise<{ ok: boolean; status: number; json: () => Promise<unknown> }>,
): StatsLoader {
  let controller: AbortController | null = null;
  let path: string | null = null;
  return {
    async load(scope, limit) {
      controller?.abort();
      const mine = new AbortController();
      controller = mine;
      path = statsPath(scope, limit);
      const response = await fetcher(`/api/levi/live/${path}`, {
        cache: "no-store",
        signal: mine.signal,
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = (await response.json()) as StatsResponse;
      return mine.signal.aborted ? null : data;
    },
    stop() {
      controller?.abort();
    },
    last: () => path,
  };
}
