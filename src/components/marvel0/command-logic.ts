import {
  inferenceMode,
  validOverlap,
  type InferenceMode,
} from "./inference-mode";
// Pure mapping for the Client command card of the marvel0: the form, the
// body of `POST /api/levi/marvel0/command/preview`, and the answer in words. The
// command is text to copy; nothing here runs, writes or starts anything.
//
// The server decides what a form may hold (ranges, the characters of a task
// instruction, the site's limits) and answers each refusal with a stable code
// per field (marvel0-failure.ts words them). This file only turns the typed text
// into numbers where the form says numbers, and says so on the field when a
// number is not one, so a typo does not cost a request.
import type { ModelsView, SiteView } from "./catalog-logic";
import {
  controlFacts,
  rtcHorizonDefault,
  type ControlFacts,
} from "./control-logic";
import type { DeployStatus } from "./deploy-logic";
import { bool, list, num, rec, str, type Worded } from "./marvel0-json";
import type { Say } from "./marvel0-logic";

export const COMMAND_SCHEMA = "levi.marvel0.command.v1";

/** The fields of the form, by the names the server uses for them. */
export const FIELDS = [
  "model_id",
  "prompt",
  "max_steps",
  "eval_num",
  "min_z",
  "rtc",
  "inference_mode",
  "vlash_overlap_steps",
  "rtc_horizon",
  "rollout_group",
  "note",
  "control_hz",
  "chunk_accel",
  "chunk_accel_rule",
  "chunk_accel_lambda",
  "chunk_accel_alpha",
] as const;
export type FieldName = (typeof FIELDS)[number];

/** What is typed. The numbers stay text until the request is made. */
export interface CommandForm {
  modelId: string;
  prompt: string;
  maxSteps: string;
  evalNum: string;
  minZ: string;
  rtc: boolean;
  inferenceMode?: InferenceMode;
  vlashOverlapSteps?: string;
  /** The RTC execute horizon, or the open-loop horizon when RTC is off. */
  horizon: string;
  group: string;
  note: string;
  /** The control rate in Hz, one of the site's options (a select, so always a
   * whole number as text); the default rate when the server offers none. */
  hz: string;
  /** In-chunk acceleration (experimental). Rules v1 and v2 are sent with plain
   * inference only; rule v3 (the phase clock) with any inference mode. */
  chunkAccel?: boolean;
  /** Its rule; absent opens on the default (v3 while the page offers it). Kept
   * while the switch is off, sent only with the switch on (v1, the backend's
   * default, as nothing). */
  chunkAccelRule?: ChunkAccelRule;
  /** Rule v3 only: the most chunk rows the phase clock advances per tick, one
   * of CHUNK_ACCEL_V3_LAMBDAS as text; empty or absent sends nothing and the
   * server's default applies (the command and the result say which). */
  chunkAccelLambda?: string;
  /** Rule v3 only: the controller's setpoint filter, one of
   * CHUNK_ACCEL_V3_ALPHAS as text; absent is CHUNK_ACCEL_V3_ALPHA_DEFAULT. */
  chunkAccelAlpha?: string;
}

// The next five constants repeat levi/marvel0/inference_mode.py: the site answer
// does not carry them, and tests/marvel0/test_chunk_accel_runtime.py keeps the
// two copies equal (change both together). Hiding a mode or all of v3 from the
// page is the backend's tuple, and this one, without it.
/** The rules of in-chunk acceleration: v1 hurries straight gripper-free chunks
 * and v2 also curved chunks and the actions before a gripper change (both are
 * `--accel-rule` of the ramp client, plain inference only); v3 is the phase
 * clock (another client, any inference mode). v1 is the backend's default and
 * is never sent. */
export const CHUNK_ACCEL_RULES = ["v1", "v2", "v3"] as const;
export type ChunkAccelRule = (typeof CHUNK_ACCEL_RULES)[number];
/** The inference modes rule v3 runs with. */
export const CHUNK_ACCEL_V3_MODES = ["plain", "rtc", "vlash"] as const;
/** `--phase-lambda-max`: the most rows of a chunk advanced per 10 Hz tick. */
export const CHUNK_ACCEL_V3_LAMBDAS = ["2.0", "2.5", "3.0"] as const;
/** `--phase-alpha`: the controller's setpoint filter, which has to be what
 * `set_clip.py --show` reads back. */
export const CHUNK_ACCEL_V3_ALPHAS = ["0.005", "0.01"] as const;
export const CHUNK_ACCEL_V3_ALPHA_DEFAULT = "0.005";

const v3Offered = (): boolean =>
  (CHUNK_ACCEL_RULES as readonly string[]).includes("v3");

/** The rule a form opens with once the switch is on: v3, the finished one
 * (owner's preference: a finished feature is on), while the page offers it. */
export const chunkAccelRuleDefault = (): ChunkAccelRule =>
  v3Offered() ? "v3" : "v1";

/** The rule of the form. Rules v1 and v2 run with plain inference only, so
 * under RTC or VLASH it is v3 whatever was chosen before (the choice is kept
 * and comes back with plain); with nothing chosen it is the default. */
export const chunkAccelRuleOf = (
  form: Pick<CommandForm, "chunkAccelRule" | "inferenceMode" | "rtc">,
): ChunkAccelRule => {
  if (inferenceMode(form.inferenceMode, form.rtc) !== "plain" && v3Offered())
    return "v3";
  return form.chunkAccelRule && CHUNK_ACCEL_RULES.includes(form.chunkAccelRule)
    ? form.chunkAccelRule
    : chunkAccelRuleDefault();
};

/** The rules that can be chosen with the form's inference mode. */
export const chunkAccelRulesFor = (
  form: Pick<CommandForm, "inferenceMode" | "rtc">,
): readonly ChunkAccelRule[] =>
  inferenceMode(form.inferenceMode, form.rtc) === "plain"
    ? CHUNK_ACCEL_RULES
    : CHUNK_ACCEL_RULES.filter((rule) => rule === "v3");

/** Whether the form's inference mode lets in-chunk acceleration be asked for:
 * plain always, RTC and VLASH when rule v3 runs with them (the backend allows
 * rules v1 and v2 with plain inference only). */
export const chunkAccelAllowed = (
  form: Pick<CommandForm, "inferenceMode" | "rtc">,
): boolean => {
  const mode = inferenceMode(form.inferenceMode, form.rtc);
  return (
    mode === "plain" ||
    (v3Offered() && (CHUNK_ACCEL_V3_MODES as readonly string[]).includes(mode))
  );
};

/** The accelerated client runs at the policy's own 10 Hz only (it refuses any
 * other rate, and so does the backend). */
export const chunkAccelRateOk = (form: Pick<CommandForm, "hz">): boolean =>
  rateOf(form as CommandForm) === DEFAULT_HZ;

/** The rate a client runs at when none is written (the backend's default). */
export const DEFAULT_HZ = 10;

/** Whether the command of this form is an accelerated one: the switch is on,
 * the inference mode and the rate allow it (the body sends it only then). */
export const chunkAccelOn = (form: CommandForm): boolean =>
  form.chunkAccel === true && chunkAccelAllowed(form) && chunkAccelRateOk(form);

/** The default rollout group of rule v3 per inference mode, as the backend
 * files it (`models.default_group`: `<model>_accel3_demo` and so on). */
const V3_GROUP_MODE: Record<InferenceMode, string> = {
  plain: "accel3",
  rtc: "rtc_accel3",
  vlash: "vlash_accel3",
};

/** The form as it opens: the site's defaults in the fields whose value guards
 * something, and nothing in the ones the server fills in (they show their
 * default as a placeholder). */
export function initialCommandForm(
  models: ModelsView,
  site: SiteView,
  modelId = "",
): CommandForm {
  const known = models.models.some((model) => model.id === modelId);
  return {
    modelId: known ? modelId : (models.models[0]?.id ?? ""),
    prompt: site.prompts.default,
    maxSteps: String(site.caps.maxSteps.default),
    evalNum: String(site.caps.evalNum.default),
    minZ: String(site.minZ.default),
    rtc: models.defaults.rtc,
    horizon: "",
    group: "",
    note: "",
    hz: String(site.control?.default ?? DEFAULT_HZ),
  };
}

/** The rule a saved run ran with: v1 when the record holds nothing the page knows. */
function savedRule(value: unknown): ChunkAccelRule {
  return CHUNK_ACCEL_RULES.find((rule) => rule === value) ?? "v1";
}

/** `{[field]: option}` for the option (text) that is the saved number; nothing
 * when it is none of them. */
function optionText<K extends "chunkAccelLambda" | "chunkAccelAlpha">(
  options: readonly string[],
  value: unknown,
  field: K,
): Partial<Record<K, string>> {
  const found =
    typeof value === "number"
      ? options.find((option) => Number(option) === value)
      : undefined;
  return found === undefined
    ? {}
    : ({ [field]: found } as Partial<Record<K, string>>);
}

/** The complete, typed dataclass form saved with a run. Missing or malformed
 * fields are not reconstructed from today's site defaults. */
export function commandFormFromRun(value: unknown): CommandForm | null {
  const item = rec(value);
  if (!item) return null;
  const modelId = item.model_id;
  const prompt = item.prompt;
  const maxSteps = item.max_steps;
  const evalNum = item.eval_num;
  const minZ = item.min_z;
  const rtc = bool(item.rtc);
  const horizon = item.rtc_horizon;
  const group = item.rollout_group;
  const note = item.note;
  const hz = item.control_hz;
  if (
    typeof modelId !== "string" ||
    !modelId.trim() ||
    typeof prompt !== "string" ||
    !prompt.trim() ||
    typeof maxSteps !== "number" ||
    !Number.isInteger(maxSteps) ||
    typeof evalNum !== "number" ||
    !Number.isInteger(evalNum) ||
    typeof minZ !== "number" ||
    !Number.isFinite(minZ) ||
    rtc === null ||
    typeof horizon !== "number" ||
    !Number.isInteger(horizon) ||
    typeof group !== "string" ||
    typeof note !== "string" ||
    typeof hz !== "number" ||
    !Number.isInteger(hz)
  )
    return null;
  return {
    modelId,
    prompt,
    maxSteps: String(maxSteps),
    evalNum: String(evalNum),
    minZ: String(minZ),
    rtc: inferenceMode(item.inference_mode, rtc) === "rtc",
    inferenceMode: inferenceMode(item.inference_mode, rtc),
    vlashOverlapSteps: String(item.vlash_overlap_steps ?? 2),
    horizon: String(horizon),
    group,
    note,
    hz: String(hz),
    // Missing (an older run) means off, and the rule v1: a run saved with the switch on keeps the rule it ran with,
    // so the page's v3 default never turns an old v1 run into a v3 one.
    ...(item.chunk_accel === true && {
      chunkAccel: true,
      chunkAccelRule: savedRule(item.chunk_accel_rule),
    }),
    ...optionText(
      CHUNK_ACCEL_V3_LAMBDAS,
      item.chunk_accel_lambda,
      "chunkAccelLambda",
    ),
    ...optionText(
      CHUNK_ACCEL_V3_ALPHAS,
      item.chunk_accel_alpha,
      "chunkAccelAlpha",
    ),
  };
}

/** What the server fills in for an empty field, as the site and the model say
 * it, for the placeholders and the hints. */
export function placeholders(
  form: CommandForm,
  models: ModelsView,
  site: SiteView,
): {
  group: string;
  horizon: string;
  horizonMax: number;
  /** The site's default when the rate lowers it for RTC; else null. */
  horizonLoweredFrom: number | null;
} {
  const model = models.models.find((entry) => entry.id === form.modelId);
  const mode = inferenceMode(form.inferenceMode, form.rtc);
  const rtc = mode === "rtc";
  const limit = rtc ? site.caps.rtcHorizon : site.caps.openLoop;
  // At a rate above the reference one an empty RTC horizon is the largest that
  // still leaves room for the inference latency (the backend does the same).
  const horizon =
    rtc && site.control
      ? rtcHorizonDefault(site.control, limit.default, rateOf(form))
      : limit.default;
  const rule = chunkAccelOn(form) ? chunkAccelRuleOf(form) : null;
  return {
    group: model
      ? rule === "v3"
        ? // rule v3 is filed apart from v1 and v2, one group per inference mode (models.default_group)
          `${model.id}_${V3_GROUP_MODE[mode]}_demo`
        : mode === "vlash"
          ? (model.groupVlash ?? `${model.id}_vlash_demo`)
          : rtc
            ? model.groupRtc
            : rule === "v2"
              ? (model.groupAccelV2 ?? `${model.id}_accel_v2_demo`)
              : rule === "v1"
                ? (model.groupAccel ?? `${model.id}_accel_demo`)
                : model.groupPlain
      : "",
    horizon: String(horizon),
    horizonMax: limit.max,
    horizonLoweredFrom: horizon < limit.default ? limit.default : null,
  };
}

/** How every note the server writes when none is given starts (command.py's
 * default note). */
export const DEFAULT_NOTE_PREFIX = "marvel0 page: ";

/** A saved run's form opened for editing every parameter. The server saved
 * the group and note it filled in for that run; they are emptied again when
 * they are those defaults, so a mode or rule chosen next gets its own default
 * group and note (review 2026-11-01: after "Edit all parameters", v1 episodes
 * were filed under the v2 run's `<model>_accel_v2_demo` with its v2 note). */
export function editableSavedForm(
  saved: CommandForm,
  models: ModelsView,
  site: SiteView,
): CommandForm {
  return {
    ...saved,
    group:
      saved.group === placeholders(saved, models, site).group
        ? ""
        : saved.group,
    note: saved.note.startsWith(DEFAULT_NOTE_PREFIX) ? "" : saved.note,
  };
}

/** The rate of the form as a number; the default one if the text is not an
 * option's (the select cannot make that, a test can). */
export function rateOf(form: CommandForm): number {
  const value = Number(form.hz);
  return Number.isInteger(value) && value > 0 ? value : DEFAULT_HZ;
}

/** What the rate of the form changes, worked out from what is typed (an empty
 * or unreadable steps or horizon field counts as its default), or null when
 * the site does not offer rates. */
export function rateFacts(
  form: CommandForm,
  models: ModelsView,
  site: SiteView,
): ControlFacts | null {
  if (!site.control) return null;
  const typedSteps = Number(form.maxSteps.trim());
  const steps =
    form.maxSteps.trim() !== "" &&
    Number.isInteger(typedSteps) &&
    typedSteps > 0
      ? typedSteps
      : site.caps.maxSteps.default;
  const typedHorizon = Number(form.horizon.trim());
  const horizon =
    form.horizon.trim() !== "" &&
    Number.isInteger(typedHorizon) &&
    typedHorizon > 0
      ? typedHorizon
      : Number(placeholders(form, models, site).horizon);
  return controlFacts(site.control, {
    hz: rateOf(form),
    steps,
    horizon,
    rtc: inferenceMode(form.inferenceMode, form.rtc) === "rtc",
  });
}

const NUMBER = /^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$/;
const WHOLE = /^[+-]?\d+$/;

type Parsed = { value: number } | { value: null; error: string } | null;

function parseNumber(text: string, whole: boolean): Parsed {
  const typed = text.trim();
  if (typed === "") return null;
  const value = (whole ? WHOLE : NUMBER).test(typed) ? Number(typed) : NaN;
  return Number.isFinite(value)
    ? { value }
    : {
        value: null,
        error: whole
          ? "marvel0.cmd.err.not_whole"
          : "marvel0.cmd.err.not_number",
      };
}

/** The body of the preview request, and the fields whose text is not a number
 * (then no request is made). An empty optional field is left out, so the
 * server's default applies; the text fields are sent as typed. */
export function commandBody(
  form: CommandForm,
  defaultHz: number = DEFAULT_HZ,
): {
  body: Record<string, unknown>;
  errors: Partial<Record<FieldName, Say>>;
} {
  const body: Record<string, unknown> = {
    model_id: form.modelId,
    prompt: form.prompt,
  };
  const errors: Partial<Record<FieldName, Say>> = {};
  const numbers: Array<[FieldName, string, boolean]> = [
    ["max_steps", form.maxSteps, true],
    ["eval_num", form.evalNum, true],
    ["min_z", form.minZ, false],
  ];
  const put = (field: FieldName, text: string, whole: boolean) => {
    const parsed = parseNumber(text, whole);
    if (!parsed) return;
    if (parsed.value === null) errors[field] = { key: parsed.error };
    else body[field] = parsed.value;
  };
  for (const [field, text, whole] of numbers) put(field, text, whole);
  const mode = inferenceMode(form.inferenceMode, form.rtc);
  body.rtc = mode === "rtc";
  body.inference_mode = mode;
  if (mode === "vlash") {
    if (!validOverlap(form.vlashOverlapSteps))
      errors.vlash_overlap_steps = { key: "marvel0.inference.overlap_error" };
    else
      body.vlash_overlap_steps = Number(form.vlashOverlapSteps?.trim() || "2");
  }
  // Sent only when on, so a command without it is the one it always was.
  if (chunkAccelOn(form)) {
    body.chunk_accel = true;
    const rule = chunkAccelRuleOf(form);
    // v1 is the backend's default: a v1 command is the one the switch always made
    if (rule !== "v1") body.chunk_accel_rule = rule;
    if (rule === "v3") {
      // Empty lambda: the server's default. The alpha is always written, since it has to match the controller.
      const lambda = form.chunkAccelLambda?.trim() ?? "";
      if (lambda !== "") {
        if ((CHUNK_ACCEL_V3_LAMBDAS as readonly string[]).includes(lambda))
          body.chunk_accel_lambda = Number(lambda);
        else
          errors.chunk_accel_lambda = {
            key: "marvel0.err.chunk_accel_lambda_invalid",
          };
      }
      const alpha =
        form.chunkAccelAlpha?.trim() || CHUNK_ACCEL_V3_ALPHA_DEFAULT;
      if ((CHUNK_ACCEL_V3_ALPHAS as readonly string[]).includes(alpha))
        body.chunk_accel_alpha = Number(alpha);
      else
        errors.chunk_accel_alpha = {
          key: "marvel0.err.chunk_accel_alpha_invalid",
        };
    }
  }
  put("rtc_horizon", form.horizon, true);
  // The default rate is left out, so the command is the one it always was.
  put("control_hz", form.hz, true);
  if (body.control_hz === defaultHz) delete body.control_hz;
  if (form.group.trim()) body.rollout_group = form.group.trim();
  if (form.note.trim()) body.note = form.note;
  return { body, errors };
}

/** A key that is equal for equal requests, to tell whether the form changed
 * after the command on show was made. */
export const bodyKey = (body: Record<string, unknown>): string =>
  JSON.stringify(body);

// -- the answer

export interface Preview {
  /** The text to copy. */
  command: string;
  /** The policy server this command needs. */
  needs: { modelId: string; rtc: boolean; inferenceMode?: InferenceMode };
  verified: boolean;
  warnings: PreviewWarning[];
  /** In-chunk acceleration of this command (`control.chunk_accel`); absent
   * when it is off or the answer does not say. v1 and v2 (v1 unless the
   * answer says otherwise) carry the ramp's speed and lag; v3, the phase
   * clock, its inference mode, the most rows per tick and the controller
   * filter it was made for. */
  chunkAccel?:
    | { speedMS: number; tauS: number; rule: "v1" | "v2" }
    | { rule: "v3"; mode: InferenceMode; lambdaMax: number; alpha: number };
}

/** A warning of the preview: `params` are the numbers its sentence holds, for
 * the warnings that are worded with them. */
export interface PreviewWarning {
  code: string;
  text: string;
  params?: Record<string, string | number>;
}

/** The answer of `POST /api/levi/marvel0/command/preview`, or null when it is not
 * a command in the format this page was written for. */
export function readPreview(data: unknown): Preview | null {
  const body = rec(data);
  if (!body || body.schema !== COMMAND_SCHEMA || body.ok !== true) return null;
  const command = typeof body.command === "string" ? body.command : "";
  const needs = rec(body.server_requirements);
  const modelId = str(needs?.model_id, 120);
  const rtc = bool(needs?.rtc);
  if (!command.trim() || !modelId || rtc === null) return null;
  const accel = rec(rec(body.control)?.chunk_accel);
  const speedMS = num(accel?.speed_m_s);
  const tauS = num(accel?.tau_s);
  const lambdaMax = num(accel?.lambda_max);
  const alpha = num(accel?.alpha);
  const accelMode = str(accel?.mode, 20);
  const phase =
    accel?.enabled === true &&
    accel.rule === "v3" &&
    lambdaMax !== null &&
    alpha !== null &&
    (accelMode === "plain" || accelMode === "rtc" || accelMode === "vlash");
  return {
    ...(phase && {
      chunkAccel: {
        rule: "v3" as const,
        mode: accelMode as InferenceMode,
        lambdaMax,
        alpha,
      },
    }),
    ...(!phase &&
      accel?.enabled === true &&
      speedMS !== null &&
      tauS !== null && {
        chunkAccel: {
          speedMS,
          tauS,
          rule: accel.rule === "v2" ? ("v2" as const) : ("v1" as const),
        },
      }),
    command,
    needs: {
      modelId,
      rtc: inferenceMode(needs?.inference_mode, rtc) === "rtc",
      inferenceMode: inferenceMode(needs?.inference_mode, rtc),
    },
    verified: bool(rec(body.model)?.verified) === true,
    warnings: list(body.warnings).flatMap((entry) => {
      const item = rec(entry);
      const code = str(item?.code, 60);
      if (!item || !code) return [];
      const params = warningParams(item.params);
      return [{ code, text: str(item.text) ?? "", ...(params && { params }) }];
    }),
  };
}

const WARNING_KEY: Record<string, string> = {
  unverified_model: "marvel0.cmd.warning.unverified_model",
  rtc_margin_low: "marvel0.cmd.warning.rtc_margin_low",
  min_z_below_task_table: "marvel0.cmd.warning.min_z_below_task_table",
  chunk_accel_experimental: "marvel0.cmd.warning.chunk_accel_experimental",
  chunk_accel_rule_v2: "marvel0.cmd.warning.chunk_accel_rule_v2",
  chunk_accel_v3: "marvel0.cmd.warning.chunk_accel_v3",
  chunk_accel_v3_alpha: "marvel0.cmd.warning.chunk_accel_v3_alpha",
};

/** The warnings whose sentence holds numbers or names the backend sends as `params`. */
const PARAM_WARNING = new Set(["chunk_accel_v3", "chunk_accel_v3_alpha"]);

/** The warnings of a control rate other than the default: their sentences hold
 * numbers, which the backend sends as `params`. */
const RATE_WARNING_KEY: Record<string, string> = {
  control_hz_not_trained: "marvel0.hz.warning.not_trained",
  control_hz_untested: "marvel0.hz.warning.untested",
  step_cap_tight: "marvel0.hz.warning.step_cap_tight",
  episode_short: "marvel0.hz.warning.episode_short",
  rtc_never_fits: "marvel0.hz.warning.rtc_never_fits",
};

function warningParams(value: unknown): Record<string, string | number> | null {
  const item = rec(value);
  if (!item) return null;
  const out: Record<string, string | number> = {};
  for (const [name, entry] of Object.entries(item)) {
    const n = num(entry);
    const text = str(entry, 60);
    if (n !== null) out[name] = n;
    else if (text !== null) out[name] = text;
  }
  return Object.keys(out).length > 0 ? out : null;
}

/** A warning of the preview. Never a refusal: the command is still made. A
 * sentence that holds numbers is worded only when the server sent them. */
export function warningSay(warning: PreviewWarning): Worded<Say> {
  const text = warning.text;
  const params = warning.params;
  // The RTC margin at a rate other than 10 Hz has its own sentence (beats of
  // that rate); at 10 Hz the server sends no params and the old one is used.
  if (warning.code === "rtc_margin_low" && params)
    return {
      say: { key: "marvel0.hz.warning.rtc_margin_low", params },
      text,
    };
  const rate = RATE_WARNING_KEY[warning.code];
  if (rate)
    return params ? { say: { key: rate, params }, text } : { say: null, text };
  const key = WARNING_KEY[warning.code];
  if (!key) return { say: null, text };
  // The sentences of rule v3 hold the mode, the lambda and the alpha; without the numbers the server's own English
  // sentence is shown instead.
  if (PARAM_WARNING.has(warning.code))
    return params ? { say: { key, params }, text } : { say: null, text };
  return { say: { key }, text };
}

// -- the policy server against the command

export type MatchKind =
  | "match"
  | "no_server"
  | "starting"
  | "other_model"
  | "needs_rtc"
  | "other_mode"
  | "by_hand"
  | "unknown";

const MATCH_KEY: Record<MatchKind, string> = {
  match: "marvel0.cmd.server.match",
  no_server: "marvel0.cmd.server.no_server",
  starting: "marvel0.cmd.server.starting",
  other_model: "marvel0.cmd.server.other_model",
  needs_rtc: "marvel0.cmd.server.needs_rtc",
  other_mode: "marvel0.cmd.server.other_mode",
  by_hand: "marvel0.cmd.server.by_hand",
  unknown: "marvel0.cmd.server.unknown",
};

/** Whether the policy server of this page can serve this command: the client
 * refuses a server that does not match its model, and an RTC client needs an
 * RTC server. Advice, not a gate: the command is made either way. */
export function serverMatch(
  command: { modelId: string; rtc: boolean; inferenceMode?: InferenceMode },
  status: DeployStatus | null,
): { kind: MatchKind; good: boolean; say: Say } {
  const make = (kind: MatchKind, params?: Record<string, string>) => ({
    kind,
    good: kind === "match",
    say: (params
      ? { key: MATCH_KEY[kind], params }
      : { key: MATCH_KEY[kind] }) as Say,
  });
  if (!status || status.state === "unknown") return make("unknown");
  if (status.state === "foreign_listener") return make("by_hand");
  if (status.state === "starting") return make("starting");
  if (status.state !== "ready") return make("no_server");
  const running = status.deploy;
  if (!running || !running.modelId || running.rtc === null)
    return make("unknown");
  if (running.modelId !== command.modelId)
    return make("other_model", { running: running.modelId });
  const wanted = inferenceMode(command.inferenceMode, command.rtc);
  const served = inferenceMode(running.inferenceMode, running.rtc);
  if (wanted !== served)
    return make(
      wanted === "rtc" && served === "plain" ? "needs_rtc" : "other_mode",
    );
  return make("match");
}
