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
}

/** The rate a client runs at when none is written (the backend's default). */
export const DEFAULT_HZ = 10;

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
  return {
    group: model
      ? mode === "vlash"
        ? (model.groupVlash ?? `${model.id}_vlash_demo`)
        : rtc
          ? model.groupRtc
          : model.groupPlain
      : "",
    horizon: String(horizon),
    horizonMax: limit.max,
    horizonLoweredFrom: horizon < limit.default ? limit.default : null,
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
  return {
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
};

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
  return key ? { say: { key }, text } : { say: null, text };
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
