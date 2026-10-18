// Pure rules of the subtask-loop view: fold a run's `subtask_*` / `vlm_*`
// notes (and the plan files they name) into rounds, loops and subtasks. No
// React here, so tests can feed it fixture journals. The local model's done
// check is a model judgement: it is shown as such, never as a label.
import type { SubtaskPlanFile, SubtasksResponse } from "./types";

export type LoopName = "forward" | "reverse";
export type SubtaskStatus =
  | "pending"
  | "running"
  | "done"
  | "retrying"
  | "failed"
  /** The model could not tell (`value: null`): the run waits for a person. */
  | "unknown"
  /** Checked, but the result waits for the operator's own label. */
  | "hidden";
/** `true` done, `false` not done, `null` could not tell, `undefined` not
 * checked yet, `"hidden"` withheld until the operator labels the episode. */
export type CheckValue = boolean | null | undefined | "hidden";

/** A frame the model looked at. `stored`: it is in the run's evidence
 * folder, so the page can show it; otherwise only its reference is known. */
export interface FrameRef {
  ref: string | null;
  sha256: string;
  stored: boolean;
}
export interface SubtaskAttempt {
  attempt: number;
  episodeId: string | null;
  instruction: string | null;
  check: CheckValue;
  frames: FrameRef[];
}
export interface SubtaskRow {
  index: number;
  pick: string | null;
  place: string | null;
  doneWhen: string | null;
  instruction: string | null;
  status: SubtaskStatus;
  attempts: SubtaskAttempt[];
}
export interface LoopView {
  round: number;
  loop: LoopName;
  task: string | null;
  planned: number | null;
  planSha256: string | null;
  /** The plan file was read and matched its digest. */
  planOk: boolean;
  /** Why the plan file could not be used (`missing`, `sha256_mismatch`...). */
  planProblem: string | null;
  /** The model refused to plan this loop. */
  refused: string | null;
  done: { subtasks: number | null; retries: number | null } | null;
  subtasks: SubtaskRow[];
}
export interface RoundView {
  round: number;
  /** Who released the forward loop (always the terminal). */
  released: string | null;
  /** Why the round was given up. */
  abandoned: string | null;
  loops: LoopView[];
}
export interface InventoryView {
  status: "proposed" | "confirmed" | "rejected";
  items: string[];
  frames: FrameRef[];
  by: string | null;
}
export type VlmState =
  | "awake"
  | "asleep"
  /** Loaded beside the policy and sent nothing while it runs (`level:
   * "stay_awake"` on a sleep note). */
  | "idle"
  | "wake_failed"
  | "wake_refused"
  | "sleep_refused";
export interface VlmView {
  state: VlmState;
  at: number | null;
  ms: number | null;
  reason: string | null;
  /** The last MemAvailable a sleep or wake reported, and when. */
  memAvailableGib: number | null;
  memAt: number | null;
}
export interface SubtaskLoopView {
  inventory: InventoryView | null;
  rounds: RoundView[];
  current: { round: number; loop: LoopName; index: number | null } | null;
  vlm: VlmView | null;
  /** How the model and the policy share the GPU (`subtask_alternation`). */
  alternation: { mode: string; ok: boolean } | null;
  /** The services terminal D started (`subtask_service_step`), in order. */
  serviceSteps: { step: number | null; key: string; ok: boolean | null }[];
  /** Notes whose detail was cut or not JSON: counted, not guessed. */
  unreadable: number;
  truncated: boolean;
}

const num = (value: unknown): number | null =>
  typeof value === "number" && Number.isFinite(value) ? value : null;
const int = (value: unknown): number | null =>
  typeof value === "number" && Number.isInteger(value) ? value : null;
const text = (value: unknown): string | null =>
  typeof value === "string" && value ? value : null;
const strings = (value: unknown): string[] =>
  Array.isArray(value)
    ? value.filter((v): v is string => typeof v === "string")
    : [];
/** Frame references: a plain digest (the fixed contract) or `{ref, sha256,
 * stored}` (what the server sends). */
export function framesOf(value: unknown): FrameRef[] {
  if (!Array.isArray(value)) return [];
  const out: FrameRef[] = [];
  for (const item of value) {
    if (typeof item === "string" && item)
      out.push({ ref: null, sha256: item, stored: true });
    else if (item && typeof item === "object") {
      const f = item as { ref?: unknown; sha256?: unknown; stored?: unknown };
      if (typeof f.sha256 === "string" && f.sha256)
        out.push({
          ref: typeof f.ref === "string" ? f.ref : null,
          sha256: f.sha256,
          stored: f.stored === true,
        });
    }
  }
  return out;
}
const loopOf = (value: unknown): LoopName | null =>
  value === "forward" || value === "reverse" ? value : null;

interface Draft extends LoopView {
  closed: boolean;
  lastSeq: number;
  /** index -> attempts in journal order, with the seq of each check. */
  started: Map<number, (SubtaskAttempt & { checkSeq: number })[]>;
  retrySeq: Map<number, number>;
}

/** Fold the notes. `hiddenEpisodes`: episodes whose check result the page
 * withholds until the operator labels them (the blind rule).
 * `maxRetries`: the session's `subtask_max_retries`, when known. */
export function foldSubtasks(
  data: SubtasksResponse | null | undefined,
  options: { hiddenEpisodes?: Set<string>; maxRetries?: number | null } = {},
): SubtaskLoopView {
  const hidden = options.hiddenEpisodes ?? new Set<string>();
  const plans: Record<string, SubtaskPlanFile> = data?.plans ?? {};
  const loops = new Map<string, Draft>();
  let inventory = null as InventoryView | null;
  let vlm = null as VlmView | null;
  let current = null as SubtaskLoopView["current"];
  let unreadable = 0;
  let alternation = null as SubtaskLoopView["alternation"];
  const serviceSteps: SubtaskLoopView["serviceSteps"] = [];
  const roundNotes = new Map<
    number,
    { released: string | null; abandoned: string | null }
  >();
  const roundNote = (round: number) => {
    const found = roundNotes.get(round) ?? { released: null, abandoned: null };
    roundNotes.set(round, found);
    return found;
  };

  const loopFor = (round: number, loop: LoopName): Draft => {
    const key = `${round}:${loop}`;
    let found = loops.get(key);
    if (!found) {
      found = {
        round,
        loop,
        task: null,
        planned: null,
        planSha256: null,
        planOk: false,
        planProblem: null,
        refused: null,
        done: null,
        subtasks: [],
        closed: false,
        lastSeq: 0,
        started: new Map(),
        retrySeq: new Map(),
      };
      loops.set(key, found);
    }
    return found;
  };

  for (const note of data?.notes ?? []) {
    const d = note.detail;
    if (note.unparsed || note.sidecar_problem) unreadable += 1;
    if (!d) continue;
    const round = int(d.round);
    const loop = loopOf(d.loop);
    const target = round !== null && loop ? loopFor(round, loop) : null;
    if (target) target.lastSeq = note.seq;
    switch (note.code) {
      case "subtask_inventory":
        inventory = {
          status: "proposed",
          items: strings(d.items),
          frames: framesOf(d.frames),
          by: null,
        };
        break;
      case "subtask_inventory_confirmed":
        inventory = {
          status: "confirmed",
          items: strings(d.items),
          frames: inventory?.frames ?? [],
          by: text(d.by),
        };
        break;
      case "subtask_inventory_rejected":
        inventory = {
          status: "rejected",
          items: inventory?.items ?? [],
          frames: inventory?.frames ?? [],
          by: null,
        };
        break;
      case "subtask_plan":
        if (target) {
          const sha = text(d.sha256);
          const file = sha ? plans[sha] : undefined;
          target.task = text(d.task);
          target.planned = int(d.n);
          target.planSha256 = sha;
          target.planOk = Boolean(file?.ok);
          target.planProblem = file
            ? file.ok
              ? null
              : (file.problem ?? "unreadable")
            : "missing";
          target.refused = null;
        }
        break;
      case "subtask_plan_refused":
        if (target) {
          target.refused = text(d.reason) ?? "refused";
          target.closed = true;
        }
        break;
      case "subtask_started": {
        const index = int(d.index);
        if (!target || index === null) break;
        const list = target.started.get(index) ?? [];
        list.push({
          attempt: int(d.attempt) ?? list.length,
          episodeId: text(d.episode_id),
          instruction: text(d.instruction),
          check: undefined,
          frames: [],
          checkSeq: 0,
        });
        target.started.set(index, list);
        current = { round: target.round, loop: target.loop, index };
        break;
      }
      case "subtask_check": {
        const index = int(d.index);
        if (!target || index === null) break;
        const list = target.started.get(index) ?? [];
        const attempt = int(d.attempt);
        const episode = text(d.episode_id);
        let row =
          list.find((a) => episode !== null && a.episodeId === episode) ??
          list.find((a) => attempt !== null && a.attempt === attempt);
        if (!row) {
          row = {
            attempt: attempt ?? list.length,
            episodeId: episode,
            instruction: null,
            check: undefined,
            frames: [],
            checkSeq: 0,
          };
          list.push(row);
          target.started.set(index, list);
        }
        const value = d.value;
        row.check =
          row.episodeId && hidden.has(row.episodeId)
            ? "hidden"
            : value === true || value === false
              ? value
              : null;
        row.frames = framesOf(d.frames);
        row.checkSeq = note.seq;
        break;
      }
      case "subtask_retry": {
        const index = int(d.index);
        if (target && index !== null) target.retrySeq.set(index, note.seq);
        break;
      }
      case "subtask_loop_done":
        if (target) {
          target.done = {
            subtasks: int(d.subtasks),
            retries: int(d.retries),
          };
          target.closed = true;
        }
        break;
      case "subtask_alternation":
        alternation = { mode: text(d.mode) ?? "sleep", ok: d.ok !== false };
        break;
      case "subtask_service_step":
        serviceSteps.push({
          step: int(d.step),
          key: text(d.key) ?? "?",
          ok: typeof d.ok === "boolean" ? d.ok : null,
        });
        break;
      case "vlm_sleep":
      case "vlm_wake": {
        const mem = num(d.mem_available_gib);
        vlm = {
          state:
            note.code === "vlm_wake"
              ? "awake"
              : d.level === "stay_awake"
                ? "idle"
                : "asleep",
          at: note.at,
          ms: num(d.ms),
          reason: d.ok === false ? "not_ok" : null,
          memAvailableGib: mem ?? vlm?.memAvailableGib ?? null,
          memAt: mem !== null ? note.at : (vlm?.memAt ?? null),
        };
        break;
      }
      case "subtask_round_released":
        if (round !== null)
          roundNote(round).released = text(d.by) ?? "terminal";
        break;
      case "subtask_round_abandoned":
        if (round !== null)
          roundNote(round).abandoned = text(d.why) ?? "abandoned";
        break;
      case "vlm_wake_failed":
      case "vlm_wake_refused":
      case "vlm_sleep_refused": {
        const mem = num(d.mem_available_gib);
        vlm = {
          state:
            note.code === "vlm_wake_failed"
              ? "wake_failed"
              : note.code === "vlm_wake_refused"
                ? "wake_refused"
                : "sleep_refused",
          at: note.at,
          ms: num(d.ms),
          reason: text(d.reason),
          memAvailableGib: mem ?? vlm?.memAvailableGib ?? null,
          memAt: mem !== null ? note.at : (vlm?.memAt ?? null),
        };
        break;
      }
    }
  }

  // A loop is over once a later loop has notes.
  const ordered = [...loops.values()].sort((a, b) => a.lastSeq - b.lastSeq);
  const latest = ordered[ordered.length - 1];
  for (const draft of ordered) {
    if (draft !== latest) draft.closed = true;
    draft.subtasks = rowsOf(draft, plans, options.maxRetries ?? null);
  }

  const rounds = new Map<number, RoundView>();
  for (const draft of [...loops.values()].sort(
    (a, b) => a.round - b.round || loopRank(a.loop) - loopRank(b.loop),
  )) {
    const round = rounds.get(draft.round) ?? {
      round: draft.round,
      released: roundNotes.get(draft.round)?.released ?? null,
      abandoned: roundNotes.get(draft.round)?.abandoned ?? null,
      loops: [],
    };
    round.loops.push(publicLoop(draft));
    rounds.set(draft.round, round);
  }
  // A round released (or given up) before any loop of it has a note.
  for (const [number, notes] of roundNotes)
    if (!rounds.has(number))
      rounds.set(number, { round: number, ...notes, loops: [] });
  if (!current && latest)
    current = { round: latest.round, loop: latest.loop, index: null };

  return {
    inventory,
    rounds: [...rounds.values()].sort((a, b) => a.round - b.round),
    current,
    vlm,
    alternation,
    serviceSteps,
    unreadable,
    truncated: Boolean(data?.truncated),
  };
}

const loopRank = (loop: LoopName) => (loop === "forward" ? 0 : 1);

function publicLoop(draft: Draft): LoopView {
  return {
    round: draft.round,
    loop: draft.loop,
    task: draft.task,
    planned: draft.planned,
    planSha256: draft.planSha256,
    planOk: draft.planOk,
    planProblem: draft.planProblem,
    refused: draft.refused,
    done: draft.done,
    subtasks: draft.subtasks,
  };
}

function rowsOf(
  draft: Draft,
  plans: Record<string, SubtaskPlanFile>,
  maxRetries: number | null,
): SubtaskRow[] {
  const file = draft.planSha256 ? plans[draft.planSha256] : undefined;
  const items = new Map<number, SubtaskRow>();
  if (file?.ok)
    for (const item of file.subtasks)
      items.set(item.index, {
        index: item.index,
        pick: item.pick,
        place: item.place,
        doneWhen: item.done_when,
        instruction: item.instruction,
        status: "pending",
        attempts: [],
      });
  else if (draft.planned !== null)
    for (let i = 0; i < draft.planned; i++)
      items.set(i, {
        index: i,
        pick: null,
        place: null,
        doneWhen: null,
        instruction: null,
        status: "pending",
        attempts: [],
      });
  for (const [index, attempts] of draft.started) {
    const row = items.get(index) ?? {
      index,
      pick: null,
      place: null,
      doneWhen: null,
      instruction: null,
      status: "pending" as SubtaskStatus,
      attempts: [],
    };
    row.attempts = attempts.map((a) => ({
      attempt: a.attempt,
      episodeId: a.episodeId,
      instruction: a.instruction,
      check: a.check,
      frames: a.frames,
    }));
    if (!row.instruction)
      row.instruction =
        attempts.find((a) => a.instruction)?.instruction ?? null;
    const last = attempts[attempts.length - 1];
    const retried = (draft.retrySeq.get(index) ?? 0) > last.checkSeq;
    const later = [...draft.started.keys()].some((other) => other > index);
    const exhausted = maxRetries !== null && attempts.length > maxRetries;
    row.status =
      last.check === undefined
        ? "running"
        : last.check === "hidden"
          ? "hidden"
          : last.check === true
            ? "done"
            : last.check === null
              ? "unknown"
              : retried && !exhausted
                ? "retrying"
                : exhausted || draft.closed || later
                  ? "failed"
                  : "retrying";
    items.set(index, row);
  }
  return [...items.values()].sort((a, b) => a.index - b.index);
}

/** Episodes whose check result the page withholds: the one the waiting card
 * hides until the operator labels it. */
export function hiddenEpisodesOf(
  card: {
    last_episode?: { episode_id?: string | null } | null;
    operator_label?: { hidden_until_labelled?: boolean } | null;
  } | null,
): Set<string> {
  const id = card?.last_episode?.episode_id;
  return card?.operator_label?.hidden_until_labelled && id
    ? new Set([id])
    : new Set();
}

export const SUBTASK_STATUS_KEY: Record<SubtaskStatus, string> = {
  pending: "automatic.subtask.status.pending",
  running: "automatic.subtask.status.running",
  done: "automatic.subtask.status.done",
  retrying: "automatic.subtask.status.retrying",
  failed: "automatic.subtask.status.failed",
  unknown: "automatic.subtask.status.unknown",
  hidden: "automatic.subtask.status.hidden",
};
export const VLM_STATE_KEY: Record<VlmState, string> = {
  awake: "automatic.subtask.vlm.awake",
  asleep: "automatic.subtask.vlm.asleep",
  wake_failed: "automatic.subtask.vlm.wake_failed",
  wake_refused: "automatic.subtask.vlm.wake_refused",
  idle: "automatic.subtask.vlm.idle",
  sleep_refused: "automatic.subtask.vlm.sleep_refused",
};

/** True for a run the page shows the subtask-loop view for. */
export function isSubtaskLoop(mode: string | null | undefined): boolean {
  return mode === "subtask_loop";
}

/** A reason code of a refusal or a VLM note in words; an unknown code is
 * shown as the server wrote it. */
export function subtaskReason(
  code: string,
  t: (key: string) => string,
): string {
  const key = `automatic.subtask.reason.${code}`;
  const words = t(key);
  return words === key ? code : words;
}
