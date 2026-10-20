// The models and the site view of the marvel0's action cards, read from
// `GET /api/levi/marvel0/models` and `GET /api/levi/marvel0/site`. Pure: no React and
// no fetching. A value of the wrong type is no answer (null), never a guess:
// the forms built from it carry limits that guard the arm, and a limit the
// page made up would be worse than none.
import type { Say } from "./marvel0-logic";
import { bool, list, num, rec, str, type Worded } from "./marvel0-json";

export const MODELS_SCHEMA = "levi.marvel0.models.v1";
export const SITE_SCHEMA = "levi.marvel0.site_view.v1";

export interface ModelRow {
  id: string;
  /** The serving config (`--policy.config`). */
  config: string;
  /** Takes a guidance scale (`--cfg-guidance-scale`). */
  cfgCapable: boolean;
  /** Takes only the known task instructions. */
  restrictedPrompts: boolean;
  /** A real-GPU serving check is recorded; false is the `unverified` label. */
  verified: boolean;
  step: number | null;
  sizeBytes: number | null;
  /** Codes, not sentences. */
  warnings: string[];
  /** The rollout group a run of this model is filed under by default. */
  groupRtc: string;
  groupPlain: string;
}

export interface SkippedRow {
  id: string;
  reason: string;
}

export interface ModelsView {
  models: ModelRow[];
  skipped: SkippedRow[];
  problems: string[];
  defaults: { rtc: boolean; fraction: number | null; cfgScale: number | null };
}

function readModel(entry: unknown): ModelRow | null {
  const item = rec(entry);
  const id = str(item?.id, 120);
  const config = str(item?.config, 120);
  if (!item || !id || !config) return null;
  const groups = rec(item.rollout_group_default);
  return {
    id,
    config,
    cfgCapable: item.cfg_capable === true,
    restrictedPrompts: item.restricted_prompts === true,
    verified: item.verified === true,
    step: num(item.step),
    sizeBytes: num(item.size_bytes),
    warnings: list(item.warnings)
      .map((warning) => str(warning, 120))
      .filter((warning): warning is string => warning !== null),
    groupRtc: str(groups?.rtc, 120) ?? `${id}_rtc_demo`,
    groupPlain: str(groups?.plain, 120) ?? `${id}_demo`,
  };
}

/** The answer of `GET /api/levi/marvel0/models`, or null when it is not in the
 * format this page was written for. */
export function readModels(data: unknown): ModelsView | null {
  const body = rec(data);
  if (!body || body.schema !== MODELS_SCHEMA || !Array.isArray(body.models))
    return null;
  const defaults = rec(body.defaults);
  return {
    models: body.models
      .map(readModel)
      .filter((model): model is ModelRow => model !== null),
    skipped: list(body.skipped).flatMap((entry) => {
      const item = rec(entry);
      const id = str(item?.id, 120);
      const reason = str(item?.reason, 80);
      return id && reason ? [{ id, reason }] : [];
    }),
    problems: list(body.problems)
      .map((problem) => str(problem, 120))
      .filter((problem): problem is string => problem !== null),
    defaults: {
      rtc: bool(defaults?.rtc) ?? true,
      fraction: num(defaults?.fraction),
      cfgScale: num(defaults?.cfg_guidance_scale),
    },
  };
}

export interface Limit {
  default: number;
  cap: number;
}
export interface HorizonLimit {
  default: number;
  max: number;
}

export interface SiteView {
  port: number | null;
  /** The height of the home pose: `--min-z` must stay below it. */
  homeZ: number;
  minZ: { floor: number; default: number };
  caps: {
    maxSteps: Limit;
    evalNum: Limit;
    rtcHorizon: HorizonLimit;
    openLoop: HorizonLimit;
    promptMaxChars: number;
    noteMaxChars: number;
    cfgScale: HorizonLimit;
  };
  prompts: {
    default: string;
    known: string[];
    /** The only instructions a restricted model takes. */
    restricted: string[];
    restrictedPrefixes: string[];
  };
  /** The client flags the site file fixes, as they appear in the command. */
  fixedFlags: string[];
  gpu: { presets: number[]; marginMib: number | null };
  unit: { stopTimeoutS: number | null; startTimeoutS: number | null };
}

function limit(part: unknown): Limit | null {
  const item = rec(part);
  const def = num(item?.default);
  const cap = num(item?.cap);
  return def === null || cap === null ? null : { default: def, cap };
}
function horizon(part: unknown): HorizonLimit | null {
  const item = rec(part);
  const def = num(item?.default);
  const max = num(item?.max);
  return def === null || max === null ? null : { default: def, max };
}

/** The answer of `GET /api/levi/marvel0/site`, or null when it is not in the
 * format this page was written for or lacks a number the forms rely on. */
export function readSite(data: unknown): SiteView | null {
  const body = rec(data);
  if (!body || body.schema !== SITE_SCHEMA) return null;
  const pose = list(rec(body.home)?.pose).map(num);
  const homeZ = pose.length >= 3 ? pose[2] : null;
  const floor = num(rec(body.min_z)?.floor);
  const minZDefault = num(rec(body.min_z)?.default);
  const caps = rec(body.caps);
  const maxSteps = limit(caps?.max_steps);
  const evalNum = limit(caps?.eval_num);
  const rtcHorizon = horizon(caps?.rtc_horizon);
  const openLoop = horizon(caps?.open_loop_horizon);
  const cfgScale = horizon(caps?.cfg_scale);
  const promptMax = num(caps?.prompt_max_chars);
  const noteMax = num(caps?.note_max_chars);
  const prompts = rec(body.prompts);
  const promptDefault = str(prompts?.default);
  if (
    homeZ === null ||
    floor === null ||
    minZDefault === null ||
    !maxSteps ||
    !evalNum ||
    !rtcHorizon ||
    !openLoop ||
    !cfgScale ||
    promptMax === null ||
    noteMax === null ||
    promptDefault === null
  )
    return null;
  const strings = (value: unknown) =>
    list(value)
      .map((entry) => str(entry))
      .filter((entry): entry is string => entry !== null);
  return {
    port: num(rec(body.server)?.port),
    homeZ,
    minZ: { floor, default: minZDefault },
    caps: {
      maxSteps,
      evalNum,
      rtcHorizon,
      openLoop,
      promptMaxChars: promptMax,
      noteMaxChars: noteMax,
      cfgScale,
    },
    prompts: {
      default: promptDefault,
      known: strings(prompts?.known),
      restricted: strings(prompts?.restricted),
      restrictedPrefixes: strings(prompts?.restricted_prefixes),
    },
    fixedFlags: strings(body.fixed_flags),
    gpu: {
      presets: list(rec(body.gpu)?.fraction_presets)
        .map(num)
        .filter((fraction): fraction is number => fraction !== null),
      marginMib: num(rec(body.gpu)?.margin_mib),
    },
    unit: {
      stopTimeoutS: num(rec(body.unit)?.stop_timeout_s),
      startTimeoutS: num(rec(body.unit)?.start_timeout_s),
    },
  };
}

// -- words

type T = (text: string) => string;

/** The text of a dropdown entry: the checkpoint, its serving config and, where
 * no real-GPU check is recorded, the `unverified` word. (A native option holds
 * text only; the card shows the same facts as a badge.) */
export function modelOptionText(model: ModelRow, t: T): string {
  return [
    model.id,
    model.config,
    ...(model.verified ? [] : [t("marvel0.deploy.model.unverified")]),
  ].join(" · ");
}

const SKIPPED_KEY: Record<string, string> = {
  not_deployable: "marvel0.deploy.skipped.not_deployable",
  not_jax: "marvel0.deploy.skipped.not_jax",
  role_not_forward: "marvel0.deploy.skipped.role_not_forward",
  config_unknown: "marvel0.deploy.skipped.config_unknown",
  no_toplevel_norm_stats: "marvel0.deploy.skipped.no_toplevel_norm_stats",
  excluded_by_site: "marvel0.deploy.skipped.excluded_by_site",
  unsafe_name: "marvel0.deploy.skipped.unsafe_name",
  name_too_long: "marvel0.deploy.skipped.name_too_long",
};

/** Why a checkpoint is not offered. */
export function skippedSay(reason: string): Worded<Say> {
  const key = SKIPPED_KEY[reason];
  return key ? { say: { key }, text: reason } : { say: null, text: reason };
}

const PROBLEM_KEY: Record<string, string> = {
  root_missing: "marvel0.deploy.problem.root_missing",
  entries_truncated: "marvel0.deploy.problem.entries_truncated",
};

/** A problem with the whole checkpoints folder. */
export function problemSay(problem: string): Worded<Say> {
  const key = PROBLEM_KEY[problem];
  return key ? { say: { key }, text: problem } : { say: null, text: problem };
}

const WARNING_KEY: Record<string, string> = {
  config_sources_disagree: "marvel0.deploy.warning.config_sources_disagree",
  cfg_checkpoint_without_cfg_config:
    "marvel0.deploy.warning.cfg_checkpoint_without_cfg_config",
  training_config_name_differs_from_serving_config:
    "marvel0.deploy.warning.training_config_name_differs_from_serving_config",
  size_is_a_lower_bound: "marvel0.deploy.warning.size_is_a_lower_bound",
  role_withheld: "marvel0.deploy.warning.role_withheld",
};

/** A warning the checkpoint listing gave a model (a code, with detail after a
 * colon for some). */
export function warningSay(code: string): Worded<Say> {
  const key = WARNING_KEY[code.split(":")[0]];
  return key ? { say: { key }, text: code } : { say: null, text: code };
}
