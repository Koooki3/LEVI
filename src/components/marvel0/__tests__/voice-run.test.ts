// The voice run's rules (voice-run.ts): which proposal runs by itself, where
// a sentence goes for each state of the hosted client, and when the start
// gate's Enter is pressed for a client the voice started.
import { describe, expect, test } from "bun:test";
import en from "@/i18n/en.json";
import { runView, type RunView } from "../run-logic";
import {
  initialSpeech,
  readMatch,
  speechReducer,
  type Proposal,
} from "../speech-logic";
import {
  GATE_WAIT_MS,
  TRANSLATED_DELAY_MS,
  VOICE_RUN_DELAY_MS,
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
const ALL = "pi05_fr3_all_step49999";
/** The switch was turned on at 0, before every proposal here was shown. */
const pick = (p: Proposal) => autoPick(p, 0);
const runs = (prompt: string) => ({
  prompt,
  delayMs: VOICE_RUN_DELAY_MS,
  translated: [],
});

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
  episode: { no?: number; counted?: number } = {},
): RunView {
  const doc = runDoc(name, (entry) => {
    if (entry.episode) Object.assign(entry.episode, episode);
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
    expect(pick(proposal(matchA(KNOWN[1])))).toEqual(runs(KNOWN[1]));
  });

  test("a generated sentence runs as its dataset original when there is one, else as generated", () => {
    expect(pick(proposal(matchC({ generated: generated() })))).toEqual(
      runs("put eggplant into blue bowl"),
    );
    // An English name said as such (not translated) has no dataset original.
    const english = generated({
      prompt: "put the kumquat in the blue bowl",
      dataset_original: null,
      slots: [
        { said: "kumquat", english: "kumquat", source: "english" },
        { said: "碗", english: "bowl", source: "lexicon" },
      ],
    });
    expect(pick(proposal(matchC({ generated: english })))).toEqual(
      runs("put the kumquat in the blue bowl"),
    );
  });

  test("tier A and a generated sentence run only when they agree", () => {
    const agree = matchA(KNOWN[2], {
      generated: generated({ dataset_original: KNOWN[2] }),
    });
    expect(pick(proposal(agree))).toEqual(runs(KNOWN[2]));
    const disagree = matchA(KNOWN[1], { generated: generated() });
    expect(pick(proposal(disagree))).toEqual({ why: "disagree" });
  });

  test("a restricted model's generated sentence is never run; its tier-A instruction is", () => {
    const restricted = matchA(KNOWN[1], {
      family: "restricted",
      generated: generated(),
    });
    expect(pick(proposal(restricted))).toEqual(runs(KNOWN[1]));
    expect(
      pick(proposal(matchC({ family: "restricted", generated: generated() }))),
    ).toEqual({ why: "choose" });
  });

  test("a flag, an edit, a cut recording, a guessed word or several candidates wait for a person", () => {
    expect(pick(proposal(matchV("stop")))).toEqual({ why: "vetoed" });
    expect(
      pick(proposal(matchV("negation", { generated: generated() }))),
    ).toEqual({ why: "vetoed" });
    expect(pick(proposal(matchA(KNOWN[1]), { edited: true }))).toEqual({
      why: "edited",
    });
    expect(pick(proposal(matchA(KNOWN[1]), { truncated: true }))).toEqual({
      why: "truncated",
    });
    const guessed = generated({
      repairs: [{ heard: "pearl", as: "pear" }],
      slots: [{ said: "pearl", english: "pear", source: "repair" }],
    });
    expect(pick(proposal(matchC({ generated: guessed })))).toEqual({
      why: "guessed",
    });
    expect(pick(proposal(matchB()))).toEqual({ why: "choose" });
    expect(pick(proposal(matchC()))).toEqual({ why: "choose" });
    for (const status of ["no_task", "unsupported", "untranslated", "unclear"])
      expect(
        pick(
          proposal(matchC({ generated: generated({ status, prompt: null }) })),
        ),
      ).toEqual({ why: "choose" });
  });
});

describe("which proposal runs by itself (review 2026-11-01)", () => {
  test("an answer shown before the switch was turned on never runs by itself", () => {
    expect(
      autoPick(proposal(matchA(KNOWN[1]), { shownAt: 999 }), 1000),
    ).toEqual({ why: "earlier" });
    expect(
      autoPick(proposal(matchA(KNOWN[1]), { shownAt: 1000 }), 1000),
    ).toEqual(runs(KNOWN[1]));
  });

  test("the other choices open, the editor open or a choice picked: the person decides", () => {
    for (const over of [
      { showAll: true },
      { editing: true },
      { picked: KNOWN[0] },
    ] as Partial<Proposal>[])
      expect(pick(proposal(matchA(KNOWN[1]), over))).toEqual({
        why: "person",
      });
  });

  test("a tier-B answer runs its generated sentence on purpose (no allowed instruction says what was heard), and without one waits", () => {
    expect(pick(proposal(matchB({ generated: generated() })))).toEqual(
      runs("put eggplant into blue bowl"),
    );
    expect(pick(proposal(matchB()))).toEqual({ why: "choose" });
  });
});

describe("which proposal runs by itself (re-review 2026-11-01)", () => {
  test("words heard before the switch stay 'earlier' when a model switch shows them again", () => {
    const first = speechReducer(initialSpeech, {
      type: "checked",
      seq: 0,
      at: 100,
      transcript: "叠盘子",
      match: readMatch(matchA(KNOWN[1]))!,
      forModel: ALL,
      edited: false,
    });
    const again = speechReducer(first, {
      type: "checked",
      seq: 0,
      at: 5000,
      transcript: "叠盘子",
      match: readMatch(matchA(KNOWN[1]))!,
      forModel: "pi05_fr3_sp_v1",
      edited: false,
    });
    expect(again.proposal?.shownAt).toBe(5000);
    expect(again.proposal?.heardAt).toBe(100);
    expect(autoPick(again.proposal!, 1000)).toEqual({ why: "earlier" });
  });

  test("a generated sentence with a name the local model translated runs after the longer countdown, with the translation", () => {
    const translated = generated({
      prompt: "put the kumquat in the blue bowl",
      dataset_original: null,
      slots: [
        { said: "金桔", english: "kumquat", source: "translation" },
        { said: "碗", english: "bowl", source: "lexicon" },
      ],
      translations: [translation()],
    });
    const expected = {
      prompt: "put the kumquat in the blue bowl",
      delayMs: TRANSLATED_DELAY_MS,
      translated: ["金桔 → kumquat"],
    };
    expect(pick(proposal(matchC({ generated: translated })))).toEqual(expected);
    expect(pick(proposal(matchB({ generated: translated })))).toEqual(expected);
    expect(TRANSLATED_DELAY_MS).toBeGreaterThan(VOICE_RUN_DELAY_MS);
    // Even if an answer ever carried a dataset original beside a translated
    // name, the sentence that runs is the one holding the shown names.
    const withOriginal = generated({
      ...translated,
      dataset_original: "put eggplant into blue bowl",
    });
    expect(pick(proposal(matchC({ generated: withOriginal })))).toEqual(
      expected,
    );
  });

  test("the first gate needs an episode record", () => {
    const wait: GateWait = {
      route: "start",
      generation: 1,
      prompt: "new",
      afterRunId: RUN,
      until: 10_000,
    };
    const gate = view("at_gate", savedForm("new"), "20261101T150000-bbbbbb");
    expect(gateStep(wait, { ...gate, episode: null }, true, 0)).toBe("wait");
  });
});

describe("where a sentence goes", () => {
  test("no client: start, after the run that was there", () => {
    expect(
      voiceRoute(view("idle_ready", null, null), true, false, "x", ALL),
    ).toEqual({ kind: "start", afterRunId: null });
    expect(
      voiceRoute(view("done", savedForm("old")), true, false, "x", ALL),
    ).toEqual({ kind: "start", afterRunId: RUN });
    expect(
      voiceRoute(view("failed", savedForm("old")), true, false, "x", ALL),
    ).toEqual({ kind: "start", afterRunId: RUN });
  });

  test("at the start gate: the same instruction presses Enter, another one changes it", () => {
    expect(
      voiceRoute(view("at_gate", savedForm("same")), true, false, "same", ALL),
    ).toEqual({ kind: "enter" });
    expect(
      voiceRoute(view("at_gate", savedForm("old")), true, false, "new", ALL),
    ).toEqual({ kind: "change", runId: RUN });
  });

  test("at the gate, a refused Enter or end is refused with the backend's reason", () => {
    const sent = voiceRoute(
      view("at_gate_enter_sent", savedForm("same")),
      true,
      false,
      "same",
      ALL,
    );
    expect(sent.kind).toBe("refuse");
    // a gate without the saved form cannot be changed
    const formless = voiceRoute(view("at_gate", null), true, false, "new", ALL);
    expect(formless.kind).toBe("refuse");
  });

  test("at the gate, another instruction for a client of another model is refused", () => {
    const other = { ...savedForm("old"), model_id: "pi05_fr3_sp_v1" };
    const route = voiceRoute(view("at_gate", other), true, false, "new", ALL);
    expect(route).toEqual({
      kind: "refuse",
      why: { say: { key: "marvel0.speech.run.why.model" }, text: "" },
    });
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
      const route = voiceRoute(
        view(name, savedForm("x")),
        true,
        false,
        "y",
        ALL,
      );
      expect(route.kind, name).toBe("refuse");
    }
    const live = voiceRoute(
      view("running", savedForm("x")),
      true,
      false,
      "y",
      ALL,
    );
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
    expect(voiceRoute(null, true, false, "x", ALL).kind).toBe("refuse");
    expect(voiceRoute(view("idle_ready"), false, false, "x", ALL).kind).toBe(
      "refuse",
    );
    expect(voiceRoute(view("idle_ready"), true, true, "x", ALL).kind).toBe(
      "refuse",
    );
  });
});

describe("Enter at the gate of the client the voice started", () => {
  const wait: GateWait = {
    route: "start",
    generation: 1,
    prompt: "new",
    afterRunId: RUN,
    until: 10_000,
  };
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

  test("only the new client's first gate: a gate after episodes this page did not see is given up", () => {
    expect(
      gateStep(
        wait,
        view("at_gate", savedForm("new"), NEXT, { no: 2 }),
        true,
        0,
      ),
    ).toBe("gone");
    expect(
      gateStep(
        wait,
        view("at_gate", savedForm("new"), NEXT, { no: 1, counted: 1 }),
        true,
        0,
      ),
    ).toBe("gone");
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
