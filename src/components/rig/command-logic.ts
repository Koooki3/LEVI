// Pure mapping for the Client command card of the Rig console: the form, the
// body of `POST /api/levi/rig/command/preview`, and the answer in words. The
// command is text to copy; nothing here runs, writes or starts anything.
//
// The server decides what a form may hold (ranges, the characters of a task
// instruction, the site's limits) and answers each refusal with a stable code
// per field (rig-failure.ts words them). This file only turns the typed text
// into numbers where the form says numbers, and says so on the field when a
// number is not one, so a typo does not cost a request.
import type { ModelsView, SiteView } from "./catalog-logic";
import type { DeployStatus } from "./deploy-logic";
import { bool, list, rec, str, type Worded } from "./rig-json";
import type { Say } from "./rig-logic";

export const COMMAND_SCHEMA = "levi.rig.command.v1";

/** The fields of the form, by the names the server uses for them. */
export const FIELDS = [
  "model_id",
  "prompt",
  "max_steps",
  "eval_num",
  "min_z",
  "rtc",
  "rtc_horizon",
  "rollout_group",
  "note",
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
  /** The RTC execute horizon, or the open-loop horizon when RTC is off. */
  horizon: string;
  group: string;
  note: string;
}

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
  };
}

/** What the server fills in for an empty field, as the site and the model say
 * it, for the placeholders and the hints. */
export function placeholders(
  form: CommandForm,
  models: ModelsView,
  site: SiteView,
): { group: string; horizon: string; horizonMax: number } {
  const model = models.models.find((entry) => entry.id === form.modelId);
  const limit = form.rtc ? site.caps.rtcHorizon : site.caps.openLoop;
  return {
    group: model ? (form.rtc ? model.groupRtc : model.groupPlain) : "",
    horizon: String(limit.default),
    horizonMax: limit.max,
  };
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
        error: whole ? "rig.cmd.err.not_whole" : "rig.cmd.err.not_number",
      };
}

/** The body of the preview request, and the fields whose text is not a number
 * (then no request is made). An empty optional field is left out, so the
 * server's default applies; the text fields are sent as typed. */
export function commandBody(form: CommandForm): {
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
  body.rtc = form.rtc;
  put("rtc_horizon", form.horizon, true);
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
  needs: { modelId: string; rtc: boolean };
  verified: boolean;
  warnings: Array<{ code: string; text: string }>;
}

/** The answer of `POST /api/levi/rig/command/preview`, or null when it is not
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
    needs: { modelId, rtc },
    verified: bool(rec(body.model)?.verified) === true,
    warnings: list(body.warnings).flatMap((entry) => {
      const item = rec(entry);
      const code = str(item?.code, 60);
      return item && code ? [{ code, text: str(item.text) ?? "" }] : [];
    }),
  };
}

const WARNING_KEY: Record<string, string> = {
  unverified_model: "rig.cmd.warning.unverified_model",
  rtc_margin_low: "rig.cmd.warning.rtc_margin_low",
  min_z_below_task_table: "rig.cmd.warning.min_z_below_task_table",
};

/** A warning of the preview. Never a refusal: the command is still made. */
export function warningSay(warning: {
  code: string;
  text: string;
}): Worded<Say> {
  const key = WARNING_KEY[warning.code];
  return key
    ? { say: { key }, text: warning.text }
    : { say: null, text: warning.text };
}

// -- the policy server against the command

export type MatchKind =
  | "match"
  | "no_server"
  | "starting"
  | "other_model"
  | "needs_rtc"
  | "by_hand"
  | "unknown";

const MATCH_KEY: Record<MatchKind, string> = {
  match: "rig.cmd.server.match",
  no_server: "rig.cmd.server.no_server",
  starting: "rig.cmd.server.starting",
  other_model: "rig.cmd.server.other_model",
  needs_rtc: "rig.cmd.server.needs_rtc",
  by_hand: "rig.cmd.server.by_hand",
  unknown: "rig.cmd.server.unknown",
};

/** Whether the policy server of this page can serve this command: the client
 * refuses a server that does not match its model, and an RTC client needs an
 * RTC server. Advice, not a gate: the command is made either way. */
export function serverMatch(
  command: { modelId: string; rtc: boolean },
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
  if (command.rtc && !running.rtc) return make("needs_rtc");
  return make("match");
}
