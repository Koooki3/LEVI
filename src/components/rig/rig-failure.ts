// The Rig console's refusals in words: what a failed call to one of the action
// routes (`deploy`, `deploy/stop`, `command/preview`, and the site and guard
// answers in front of them) says to a person, in the page's language. The
// server's stable `code` and the facts it sends along (`extra`) are worded from
// the catalogue; a code the page does not know shows the server's sentence as
// written (English, whatever the page's language) and is marked so by the
// caller. Pure: no React and no fetching.
import type { ApiFailure } from "./deploy-api";
import type { SiteView } from "./catalog-logic";
import { list, num, rec, str, type Worded } from "./rig-json";
import type { Say } from "./rig-logic";

export interface FailureContext {
  /** The call was a write: with no answer, it may still have been accepted. */
  write?: boolean;
  /** The site view, for the limits a refusal names (null: not read). */
  site?: SiteView | null;
}

type Params = Record<string, string | number | Say>;

/** The characters a task instruction or a note cannot contain (besides the
 * quotes and the backslash the sentence names): the page lists them, so it is
 * a parameter of the sentence, not part of its text. */
export const FORBIDDEN_CHARS = "' \" \\ $ ` !";

const BY_CODE: Record<string, string> = {
  network: "rig.err.network",
  validation: "rig.err.validation",
  person_only: "rig.err.person_only",
  ui_token: "rig.err.ui_token",
  site_missing: "rig.err.site_missing",
  site_invalid: "rig.err.site_invalid",
  request_id_invalid: "rig.err.request_id_invalid",
  confirm_required: "rig.err.confirm_required",
  model_unknown: "rig.err.model_unknown",
  fraction_not_allowed: "rig.err.fraction_not_allowed",
  cfg_scale_not_allowed: "rig.err.cfg_scale_not_allowed",
  cfg_scale_out_of_range: "rig.err.cfg_scale_out_of_range",
  unverified_not_accepted: "rig.err.unverified_not_accepted",
  busy: "rig.err.busy",
  request_id_used: "rig.err.request_id_used",
  state_dir_unusable: "rig.err.state_dir_unusable",
  checkpoint_invalid: "rig.err.checkpoint_invalid",
  runtime_files_missing: "rig.err.runtime_files_missing",
  source_file_missing: "rig.err.source_file_missing",
  source_changed: "rig.err.source_changed",
  net_table_unreadable: "rig.err.net_table_unreadable",
  port_taken: "rig.err.port_taken",
  already_running: "rig.err.already_running",
  client_connected: "rig.err.client_connected",
  systemd_unreadable: "rig.err.systemd_unreadable",
  policy_unit_exists: "rig.err.policy_unit_exists",
  arm_lock_held: "rig.err.arm_lock_held",
  arm_lock_unknown: "rig.err.arm_lock_unknown",
  aeri_run_live: "rig.err.aeri_run_live",
  aeri_registry_unreadable: "rig.err.aeri_registry_unreadable",
  gpu_lock_foreign: "rig.err.gpu_lock_foreign",
  env_unreadable: "rig.err.env_unreadable",
  gpu_unreadable: "rig.err.gpu_unreadable",
  gpu_insufficient: "rig.err.gpu_insufficient",
  start_failed: "rig.err.start_failed",
  start_unconfirmed: "rig.err.start_unconfirmed",
  nothing_to_stop: "rig.err.nothing_to_stop",
  foreign_listener_not_stoppable: "rig.err.foreign_listener_not_stoppable",
  unit_mismatch: "rig.err.unit_mismatch",
  stop_failed: "rig.err.stop_failed",
  stop_incomplete: "rig.err.stop_incomplete",
  invalid_type: "rig.err.invalid_type",
  min_z_not_positive: "rig.err.min_z_not_positive",
  min_z_below_floor: "rig.err.min_z_below_floor",
  min_z_above_home: "rig.err.min_z_above_home",
  prompt_unknown_for_model: "rig.err.prompt_unknown_for_model",
  max_steps_out_of_range: "rig.err.out_of_range",
  eval_num_out_of_range: "rig.err.out_of_range",
  rtc_horizon_out_of_range: "rig.err.out_of_range",
  rollout_group_invalid: "rig.err.rollout_group_invalid",
  unknown_field: "rig.err.unknown_field",
  command_self_check: "rig.err.command_self_check",
};

const PROMPT_REASON: Record<string, string> = {
  empty: "rig.err.prompt.empty",
  too_long: "rig.err.prompt.too_long",
  forbidden_char: "rig.err.prompt.forbidden_char",
  leading_dash: "rig.err.prompt.leading_dash",
  control_char: "rig.err.prompt.control_char",
};
const NOTE_REASON: Record<string, string> = {
  empty: "rig.err.note.empty",
  too_long: "rig.err.note.too_long",
  forbidden_char: "rig.err.note.forbidden_char",
  leading_dash: "rig.err.note.leading_dash",
  control_char: "rig.err.note.control_char",
};

const ACTION: Record<string, string> = {
  deploy: "rig.deploy.action.deploy",
  stop: "rig.deploy.action.stop",
};

/** A fact as text; "?" when the server did not send it, so a sentence never
 * shows a bare `{placeholder}`. */
function show(value: unknown): string | number {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value) return value;
  return "?";
}

function paramsOf(f: ApiFailure): Params {
  const x = f.extra;
  switch (f.code) {
    case "site_missing":
      return { path: show(x.looked_at) };
    case "confirm_required":
      return { phrase: show(x.phrase) };
    case "source_changed":
      return { file: show(x.file) };
    case "port_taken":
      return { port: show(x.port) };
    case "client_connected":
      return { n: show(x.established) };
    case "policy_unit_exists":
      return {
        units: list(x.units)
          .map((unit) => str(unit, 120))
          .filter((unit): unit is string => unit !== null)
          .join(", "),
      };
    case "aeri_run_live":
      return { run_id: show(x.run_id) };
    case "gpu_insufficient": {
      const fraction = num(x.fraction);
      return {
        fraction: fraction === null ? "?" : fraction.toFixed(2),
        total: show(x.total_mib),
        required: show(x.required_mib),
        reserved: show(x.reserved_mib),
        margin: show(x.margin_mib),
        free: show(x.free_mib),
      };
    }
    case "stop_failed":
      return { code: show(x.exit_code) };
    case "stop_incomplete":
      return { unit: show(x.unit) };
    case "busy": {
      const action = str(rec(x.operation)?.action, 20);
      return {
        action: {
          key: (action && ACTION[action]) || "rig.deploy.action.other",
        },
      };
    }
    case "min_z_below_floor":
      return { floor: show(x.floor) };
    case "min_z_above_home":
      return { home: show(x.home) };
    case "max_steps_out_of_range":
    case "eval_num_out_of_range":
    case "rtc_horizon_out_of_range":
      return { min: show(x.min), max: show(x.max) };
    default:
      if (f.code.startsWith("http_")) return { status: f.status };
      return {};
  }
}

/** The key of the sentence for a refusal about a task instruction or a note:
 * by its `reason`, else the general one. */
function reasonKey(
  f: ApiFailure,
  ctx: FailureContext,
): [string, Params] | null {
  const reason = str(f.extra.reason, 40) ?? "";
  const site = ctx.site;
  if (f.code === "prompt_invalid")
    return [
      PROMPT_REASON[reason] ?? "rig.err.prompt.generic",
      { n: site?.caps.promptMaxChars ?? 300, chars: FORBIDDEN_CHARS },
    ];
  if (f.code === "note_invalid")
    return [
      NOTE_REASON[reason] ?? "rig.err.note.generic",
      { n: site?.caps.noteMaxChars ?? 200, chars: FORBIDDEN_CHARS },
    ];
  return null;
}

/** A failed call in words. A write with no answer says it is not known whether
 * the request arrived (`ctx.write`). */
export function sayFailure(
  f: ApiFailure,
  ctx: FailureContext = {},
): Worded<Say> {
  const text = f.message;
  const reasoned = reasonKey(f, ctx);
  if (reasoned) return { say: { key: reasoned[0], params: reasoned[1] }, text };
  if (f.code === "network" && ctx.write)
    return { say: { key: "rig.err.network_write" }, text };
  if (f.code.startsWith("http_"))
    return {
      say: { key: "rig.err.http", params: paramsOf(f) },
      text,
    };
  const key = BY_CODE[f.code];
  if (!key) return { say: null, text };
  const params = paramsOf(f);
  return {
    say: Object.keys(params).length ? { key, params } : { key },
    text,
  };
}

/** The refusal of a stop. A client that is connected is worded for the stop
 * dialog, which has the override phrase; the rest is as for any call. */
export function sayStopRefusal(
  f: ApiFailure,
  ctx: FailureContext = {},
): Worded<Say> {
  if (f.code === "client_connected")
    return {
      say: {
        key: "rig.err.client_connected_stop",
        params: paramsOf(f),
      },
      text: f.message,
    };
  return sayFailure(f, ctx);
}

/** The server's own words that go with a refusal worth showing (English): what
 * `systemd-run` said when it did not start the unit, and what a proxy or a
 * broken network said when there was no usable answer. */
export function failureDetail(f: ApiFailure): string | null {
  if (f.code === "start_failed") return str(f.extra.detail);
  if (f.code === "network" || f.code.startsWith("http_")) return str(f.message);
  return null;
}

const FORM_CODES = new Set([
  "invalid_type",
  "min_z_not_positive",
  "min_z_below_floor",
  "min_z_above_home",
  "prompt_unknown_for_model",
  "prompt_invalid",
  "max_steps_out_of_range",
  "eval_num_out_of_range",
  "rtc_horizon_out_of_range",
  "rollout_group_invalid",
  "note_invalid",
  "unknown_field",
  "model_unknown",
]);

/** The refusals of a form, by the field each is about: LEVI's own form codes
 * name their field, and FastAPI lists the fields of a body it did not accept.
 * A refusal about nothing in the form (the site file, the guard, a failed
 * self-check) is on no field. */
export function fieldMessages(
  f: ApiFailure,
  ctx: FailureContext,
): Record<string, Worded<Say>> {
  const out: Record<string, Worded<Say>> = {};
  if (f.code === "validation") {
    for (const entry of f.fields)
      out[entry.field] = {
        say: { key: BY_CODE.invalid_type },
        text: entry.message,
      };
    return out;
  }
  if (f.field && FORM_CODES.has(f.code)) out[f.field] = sayFailure(f, ctx);
  return out;
}
