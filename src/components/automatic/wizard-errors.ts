// What a refused request says, in words a person can act on. The server's
// answer carries a contract code, a sentence and (for a job that does not
// load) one entry per field; none of that is shown as it came when the page
// knows it. A code or message the page does not know still shows the
// server's own sentence, so nothing is lost.
import { ApiError, type FieldError } from "./wizard-api";

type T = (key: string) => string;

/** Codes of a refused request -> catalogue key. */
const CODE_KEYS: Record<string, string> = {
  job_invalid: "automatic.wizard.error.job_invalid",
  job_exists: "automatic.wizard.error.job_exists",
  job_outside_roots: "automatic.wizard.error.job_outside_roots",
  no_job_root: "automatic.wizard.error.no_job_root",
  job_write_failed: "automatic.wizard.error.job_write_failed",
  plan_changed: "automatic.wizard.error.plan_stale",
  token_expired: "automatic.wizard.error.plan_stale",
  token_invalid: "automatic.wizard.error.plan_stale",
  network: "automatic.wizard.error.network",
  controller_down: "automatic.campaign.error.controller_down",
  controller_alive: "automatic.campaign.error.controller_alive",
  not_waiting: "automatic.campaign.error.not_waiting",
  stale_sequence: "automatic.campaign.error.stale_sequence",
  robot_busy: "automatic.campaign.error.robot_busy",
  campaign_ended: "automatic.campaign.error.campaign_ended",
  not_started: "automatic.campaign.error.not_started",
  not_guided: "automatic.campaign.error.not_guided",
  episode_unknown: "automatic.campaign.error.episode_unknown",
  card_unknown: "automatic.campaign.error.card_unknown",
  card_invalid: "automatic.campaign.error.card_invalid",
  busy: "automatic.campaign.error.busy",
  person_only: "automatic.campaign.error.person_only",
};

/** Field paths of the job -> the name the form uses for them. */
const FIELD_KEYS: Record<string, string> = {
  "task.instruction": "automatic.wizard.settings.instruction",
  "run.max_steps": "automatic.wizard.settings.max_steps",
  "run.episodes": "automatic.wizard.settings.episodes",
  "policy_forward.checkpoint_id": "automatic.wizard.policy.pick_one",
  "policy_reset.checkpoint_id": "automatic.wizard.reset.legend",
  "reset.strategy": "automatic.wizard.reset.legend",
  "reset.scene_check": "automatic.wizard.reset.attested",
  "termination.allow_early_stop": "automatic.wizard.settings.early_stop",
};

/** Messages the page recognises (matched on the server's sentence, which
 * names the setting by its file spelling). */
const MESSAGE_RULES: { match: RegExp; key: string }[] = [
  {
    match: /operator_attested needs (the )?task\.initial_state_spec/i,
    key: "automatic.wizard.error.attested_needs_contract",
  },
  {
    match: /task\.initial_state_spec/i,
    key: "automatic.wizard.error.no_contract",
  },
];

export function codeKey(code: string): string | null {
  const name = code.toLowerCase().replace(/^e_/, "");
  return CODE_KEYS[name] ?? null;
}

/** One field error as a sentence. */
export function fieldText(error: FieldError, t: T): string {
  for (const rule of MESSAGE_RULES)
    if (rule.match.test(error.message)) return t(rule.key);
  const fieldKey = error.field ? FIELD_KEYS[error.field] : undefined;
  // A path of the file ("$", "run.x") is not shown when it has no name here.
  return fieldKey ? `${t(fieldKey)}: ${error.message}` : error.message;
}

export type Failure = { message: string; fields: string[]; code: string };

/** A refused request as the page shows it. */
export function failureOf(error: unknown, t: T): Failure {
  if (error instanceof ApiError) {
    const fields = error.errors.map((e) => fieldText(e, t));
    // The summary sentence is the page's own when the code is known and the
    // fields below say what is wrong; an unknown code keeps the server's.
    const key = codeKey(error.code);
    return {
      message: key ? t(key) : error.message,
      code: error.code,
      fields: [...new Set(fields)],
    };
  }
  return {
    message: error instanceof Error ? error.message : String(error),
    code: "",
    fields: [],
  };
}

/** The same, as one line (the campaign page shows a single sentence). */
export function failureText(error: unknown, t: T): string {
  const { message, fields } = failureOf(error, t);
  return [message, ...fields].join(" · ");
}
