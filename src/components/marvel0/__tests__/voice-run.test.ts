// The voice run's rules (voice-run.ts): which proposal runs by itself, where
// a sentence goes for each state of the hosted client, and when the start
// gate's Enter is pressed for a client the voice started.
import { describe, expect, test } from "bun:test";
import en from "@/i18n/en.json";
import { runView, type RunView } from "../run-logic";
import { readMatch, type Proposal } from "../speech-logic";
import {
  GATE_WAIT_MS,
  autoPick,
  gateStep,
  voiceRoute,
  type GateWait,
} from "../voice-run";
import { runDoc } from "./ctl-fixtures";
import {
  KNOWN,
  generated,
  matchA,
  matchB,
  matchC,
  matchV,
  translation,
} from "./speech-fixtures";

const known = new Set(Object.keys(en));

function proposal(
  match: Record<string, unknown>,
  over: Partial<Proposal> = {},
): Proposal {
  const read = readMatch(match);
  if (!read) throw new Error("the match fixture does not read");
  return {
    id: 1,
    transcript: "heard",
    edited: false,
    language: "Chinese",
    model: "qwen3-asr",
    match: read,
    forModel: "pi05_fr3_all_step49999",
    shownAt: 0,
    armed: true,
    checking: false,
    picked: null,
    showAll: false,
    editing: false,
    draft: "heard",
    truncated: false,
    ...over,
  };
}

const RUN = "20261019T073000-4f2a91";
function view(
  name: string,
  form: Record<string, unknown> | null = null,
  runId: string | null = RUN,
): RunView {
  const doc = runDoc(name, (entry) => {
    if (form || runId)
      entry.run = {
        run_id: runId,
        phase: entry.state === "done" ? "ended" : "started",
        lock_released: entry.state === "done",
        form,
      };
  });
  const read = runView(doc, known);
  if (!read) throw new Error(`run view ${name} does not read`);
  return read;
}
const savedForm = (prompt: string) => ({
  model_id: "pi05_fr3_all_step49999",
  prompt,
  max_steps: 500,
  eval_num: 5,
  min_z: 0.025,
  rtc: true,
  rtc_horizon: 10,
  rollout_group: "g",
  note: "",
  control_hz: 10,
});

describe("which proposal runs by itself", () => {
  test("a tier-A instruction runs", () => {
    expect(autoPick(proposal(matchA(KNOWN[1])))).toEqual({
      prompt: KNOWN[1],
    });
  });

  test("a generated sentence runs as its dataset original when there is one, else as generated", () => {
    expect(autoPick(proposal(matchC({ generated: generated() })))).toEqual({
      prompt: "put eggplant into blue bowl",
    });
    const translated = generated({
      prompt: "put the kumquat in the blue bowl",
      dataset_original: null,
      slots: [
        { said: "金桔", english: "kumquat", source: "translation" },
        { said: "碗", english: "bowl", source: "lexicon" },
      ],
      translations: [translation()],
    });
    expect(autoPick(proposal(matchC({ generated: translated })))).toEqual({
      prompt: "put the kumquat in the blue bowl",
    });
  });

  test("tier A and a generated sentence run only when they agree", () => {
    const agree = matchA(KNOWN[2], {
      generated: generated({ dataset_original: KNOWN[2] }),
    });
    expect(autoPick(proposal(agree))).toEqual({ prompt: KNOWN[2] });
    const disagree = matchA(KNOWN[1], { generated: generated() });
    expect(autoPick(proposal(disagree))).toEqual({ why: "disagree" });
  });

  test("a restricted model's generated sentence is never run; its tier-A instruction is", () => {
    const restricted = matchA(KNOWN[1], {
      family: "restricted",
      generated: generated(),
    });
    expect(autoPick(proposal(restricted))).toEqual({ prompt: KNOWN[1] });
    expect(
      autoPick(
        proposal(matchC({ family: "restricted", generated: generated() })),
      ),
    ).toEqual({ why: "choose" });
  });

  test("a flag, an edit, a cut recording, a guessed word or several candidates wait for a person", () => {
    expect(autoPick(proposal(matchV("stop")))).toEqual({ why: "vetoed" });
    expect(
      autoPick(proposal(matchV("negation", { generated: generated() }))),
    ).toEqual({ why: "vetoed" });
    expect(autoPick(proposal(matchA(KNOWN[1]), { edited: true }))).toEqual({
      why: "edited",
    });
    expect(autoPick(proposal(matchA(KNOWN[1]), { truncated: true }))).toEqual({
      why: "truncated",
    });
    const guessed = generated({
      repairs: [{ heard: "pearl", as: "pear" }],
      slots: [{ said: "pearl", english: "pear", source: "repair" }],
    });
    expect(autoPick(proposal(matchC({ generated: guessed })))).toEqual({
      why: "guessed",
    });
    expect(autoPick(proposal(matchB()))).toEqual({ why: "choose" });
    expect(autoPick(proposal(matchC()))).toEqual({ why: "choose" });
    for (const status of ["no_task", "unsupported", "untranslated", "unclear"])
      expect(
        autoPick(
          proposal(matchC({ generated: generated({ status, prompt: null }) })),
        ),
      ).toEqual({ why: "choose" });
  });
});

describe("where a sentence goes", () => {
  test("no client: start, after the run that was there", () => {
    expect(
      voiceRoute(view("idle_ready", null, null), true, false, "x"),
    ).toEqual({ kind: "start", afterRunId: null });
    expect(
      voiceRoute(view("done", savedForm("old")), true, false, "x"),
    ).toEqual({ kind: "start", afterRunId: RUN });
    expect(
      voiceRoute(view("failed", savedForm("old")), true, false, "x"),
    ).toEqual({ kind: "start", afterRunId: RUN });
  });

  test("at the start gate: the same instruction presses Enter, another one changes it", () => {
    expect(
      voiceRoute(view("at_gate", savedForm("same")), true, false, "same"),
    ).toEqual({ kind: "enter" });
    expect(
      voiceRoute(view("at_gate", savedForm("old")), true, false, "new"),
    ).toEqual({ kind: "change", runId: RUN });
  });

  test("at the gate, a refused Enter or end is refused with the backend's reason", () => {
    const sent = voiceRoute(
      view("at_gate_enter_sent", savedForm("same")),
      true,
      false,
      "same",
    );
    expect(sent.kind).toBe("refuse");
    // a gate without the saved form cannot be changed
    const formless = voiceRoute(view("at_gate", null), true, false, "new");
    expect(formless.kind).toBe("refuse");
  });

  test("an episode that runs, waits for its label or is frozen refuses, never queues", () => {
    for (const name of [
      "running",
      "awaiting_label",
      "homing",
      "starting",
      "finishing_episode",
      "held",
      "lost",
      "preflight",
    ]) {
      const route = voiceRoute(view(name, savedForm("x")), true, false, "y");
      expect(route.kind, name).toBe("refuse");
    }
    const live = voiceRoute(view("running", savedForm("x")), true, false, "y");
    expect(live).toEqual({
      kind: "refuse",
      why: {
        say: {
          key: "marvel0.speech.run.why.live",
          params: { state: { key: "marvel0.run.state.running" } },
        },
        text: "",
      },
    });
  });

  test("a stale read, a missing read or another write in flight refuses", () => {
    expect(voiceRoute(null, true, false, "x").kind).toBe("refuse");
    expect(voiceRoute(view("idle_ready"), false, false, "x").kind).toBe(
      "refuse",
    );
    expect(voiceRoute(view("idle_ready"), true, true, "x").kind).toBe("refuse");
  });
});

describe("Enter at the gate of the client the voice started", () => {
  const wait: GateWait = { prompt: "new", afterRunId: RUN, until: 10_000 };
  const NEXT = "20261101T150000-aaaaaa";

  test("waits while the old run is still the one shown, or the new one starts", () => {
    expect(gateStep(wait, view("at_gate", savedForm("old")), true, 0)).toBe(
      "wait",
    );
    expect(gateStep(wait, view("done", savedForm("old")), true, 0)).toBe(
      "wait",
    );
    expect(
      gateStep(wait, view("starting", savedForm("new"), NEXT), true, 0),
    ).toBe("wait");
    expect(
      gateStep(
        wait,
        view("starting_gate_unconfirmed", savedForm("new"), NEXT),
        true,
        0,
      ),
    ).toBe("wait");
    expect(
      gateStep(wait, view("at_gate", savedForm("new"), NEXT), false, 0),
    ).toBe("wait");
  });

  test("presses Enter at the new run's gate with the same instruction", () => {
    expect(
      gateStep(wait, view("at_gate", savedForm("new"), NEXT), true, 0),
    ).toBe("enter");
    // the gate says Enter is not offered (already sent): wait, never press
    expect(
      gateStep(
        wait,
        view("at_gate_enter_sent", savedForm("new"), NEXT),
        true,
        0,
      ),
    ).toBe("wait");
  });

  test("gives up on another instruction, a failed or frozen client, and after the wait", () => {
    expect(
      gateStep(wait, view("at_gate", savedForm("other"), NEXT), true, 0),
    ).toBe("gone");
    expect(
      gateStep(wait, view("failed_no_gate", savedForm("new"), NEXT), true, 0),
    ).toBe("gone");
    expect(gateStep(wait, view("held", savedForm("new"), NEXT), true, 0)).toBe(
      "gone",
    );
    expect(
      gateStep(wait, view("at_gate", savedForm("new"), NEXT), true, 10_000),
    ).toBe("late");
    expect(GATE_WAIT_MS).toBe(120_000);
  });
});
