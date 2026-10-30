import { describe, expect, test } from "bun:test";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import { errorOf } from "../deploy-api";
import type { Say } from "../marvel0-logic";
import { sayText } from "../marvel0-say";
import {
  DIFFERENCE_CODES,
  GATE_REASONS,
  SPEECH_CODES,
  availability,
  canUse,
  candidateOf,
  describeCandidate,
  effectiveTier,
  engineSay,
  failureWords,
  generatedPrompts,
  heardSay,
  initialSpeech,
  languageSay,
  micSay,
  plain,
  proposalView,
  readHeard,
  readMatch,
  readMatched,
  readSpeechStatus,
  reasonWords,
  sayParts,
  speechReducer,
  topChoices,
  visiblePrompts,
  type Heard,
  type HeardReason,
  type SpeechAction,
  type SpeechState,
  type StatusRead,
} from "../speech-logic";
import {
  KNOWN,
  RESTRICTED,
  candidate,
  generated,
  heardAnswer,
  matchA,
  matchB,
  matchC,
  matchV,
  matchedAnswer,
  statusAnswer,
} from "./speech-fixtures";

const catalogue = (lang: "en" | "zh") => (key: string) =>
  (lang === "en" ? en : zh)[key as keyof typeof en] ?? key;
const words = (say: Say, lang: "en" | "zh" = "en") =>
  sayText(say, catalogue(lang));

const match = (raw: Record<string, unknown>) => readMatch(raw)!;
const heardOf = (raw: Record<string, unknown>, id = "req-12345678") =>
  readHeard(heardAnswer(id, raw), id) as Heard;

function run(actions: SpeechAction[], from: SpeechState = initialSpeech) {
  return actions.reduce(speechReducer, from);
}
/** A state with a proposal that was made at `at`. */
function withProposal(raw: Record<string, unknown>, at = 1000, seq = 1) {
  return run([
    { type: "begin", seq },
    { type: "uploading", seq },
    {
      type: "answer",
      seq,
      at,
      heard: heardOf(raw),
      forModel: "pi05_fr3_all_step49999",
      truncated: false,
    },
  ]);
}

describe("plain", () => {
  test("removes bidi, zero-width and control characters, folds white space and cuts", () => {
    expect(plain("a‮b​c\u0000d\te\n f")).toBe("a b c d e f");
    expect(plain("  ")).toBeNull();
    expect(plain(5)).toBeNull();
    expect(plain("x".repeat(500), 20)).toHaveLength(20);
  });
});

describe("readSpeechStatus", () => {
  test("reads the answer of GET speech", () => {
    const status = readSpeechStatus(
      statusAnswer({
        state: "failed",
        reason: { code: "speech_failed", text: "The worker exited" },
        gate: { allowed: false, reasons: ["client_connected", "arm_in_use"] },
      }),
    )!;
    expect(status.state).toBe("failed");
    expect(status.reason).toEqual({
      code: "speech_failed",
      text: "The worker exited",
    });
    expect(status.gate).toEqual({
      allowed: false,
      reasons: ["client_connected", "arm_in_use"],
    });
    expect(status.installed).toEqual({ worker: true, model: true });
    expect(status.maxSeconds).toBe(10);
  });

  test("refuses an answer in another format rather than guessing", () => {
    expect(readSpeechStatus(null)).toBeNull();
    expect(readSpeechStatus(statusAnswer({ schema: "other" }))).toBeNull();
    expect(readSpeechStatus(statusAnswer({ state: "sleeping" }))).toBeNull();
    expect(readSpeechStatus(statusAnswer({ gate: null }))).toBeNull();
    expect(
      readSpeechStatus(statusAnswer({ gate: { reasons: [] } })),
    ).toBeNull();
    expect(readSpeechStatus(statusAnswer({ installed: null }))).toBeNull();
  });
});

describe("readMatch", () => {
  test("reads each tier", () => {
    expect(match(matchA()).tier).toBe("A");
    expect(match(matchB()).alternatives).toHaveLength(2);
    expect(match(matchC()).best).toBeNull();
    expect(effectiveTier(match(matchV("stop")))).toBe("V");
  });

  test("a sentence the allowed list does not hold is never offered", () => {
    expect(readMatch(matchA("rm -rf /"))).toBeNull(); // tier A without its sentence
    const some = match(
      matchB({ alternatives: [candidate("something else", { score: 70 })] }),
    );
    expect(some.alternatives).toEqual([]);
    expect(topChoices(some)).toEqual([KNOWN[1]]);
  });

  test("a tier that promises a sentence without one cannot be trusted", () => {
    expect(readMatch(matchA(KNOWN[0], { best: null }))).toBeNull();
    expect(readMatch(matchB({ best: null }))).toBeNull();
  });

  test("any flag makes the result a V result, an unknown flag counts as unsafe", () => {
    const flagged = match(matchA(KNOWN[0], { flag: "stop" }));
    expect(effectiveTier(flagged)).toBe("V");
    expect(proposalView({ match: flagged } as never)).toBe("V");
    expect(match(matchA(KNOWN[0], { flag: "wait a moment" })).flag).toBe(
      "unsafe",
    );
    expect(effectiveTier(match(matchV(null)))).toBe("V");
  });

  test("keeps the family and refuses an unknown one", () => {
    expect(
      match(matchA(KNOWN[0], { family: "restricted", allowed: RESTRICTED }))
        .family,
    ).toBe("restricted");
    expect(readMatch(matchA(KNOWN[0], { family: "anything" }))).toBeNull();
    expect(readMatch({ ...matchA(), tier: "Z" })).toBeNull();
  });

  test("cleans every text, drops duplicates and cuts the lists", () => {
    const dirty = match(
      matchB({
        allowed: [...KNOWN, ` ${KNOWN[0]}‮`],
        best: candidate(KNOWN[1], {
          fixes: [
            { heard: "egg​plant", as: "eggplant" },
            { heard: "", as: "x" },
          ],
          differences: Array.from({ length: 9 }, () => ({
            code: "order",
            said: "a‮b",
            expected: "c",
          })),
        }),
      }),
    );
    expect(dirty.allowed).toEqual(KNOWN);
    expect(dirty.best?.fixes).toEqual([{ heard: "egg plant", as: "eggplant" }]);
    expect(dirty.best?.differences).toHaveLength(4);
    expect(dirty.best?.differences[0].said).toBe("a b");
  });

  test("readMatched needs its own schema", () => {
    expect(readMatched(matchedAnswer(matchA()))?.tier).toBe("A");
    expect(
      readMatched({ schema: "levi.marvel0.speech.v1", match: matchA() }),
    ).toBeNull();
  });
});

describe("readHeard", () => {
  test("an answer whose request id is another one is not this request's answer", () => {
    expect(readHeard(heardAnswer("req-aaaaaaaa"), "req-bbbbbbbb")).toBeNull();
    expect(
      readHeard(
        heardAnswer("req-aaaaaaaa", matchA(), { ok: false }),
        "req-aaaaaaaa",
      ),
    ).toBeNull();
    expect(
      readHeard(
        heardAnswer("req-aaaaaaaa", matchA(), { schema: "x" }),
        "req-aaaaaaaa",
      ),
    ).toBeNull();
  });

  test("reads what was heard, with the language and the model", () => {
    const heard = heardOf(matchA(), "req-aaaaaaaa");
    expect(heard).toMatchObject({
      heard: true,
      transcript: "pick the eggplant in the blue plate",
      language: "English",
      model: "qwen3-asr-1.7b-hf",
    });
    expect(heardOf(matchA()).match?.tier).toBe("A");
    const chinese = readHeard(
      heardAnswer("req-aaaaaaaa", matchA(), { language: "Chinese" }),
      "req-aaaaaaaa",
    )!;
    expect(chinese.language).toBe("Chinese");
    const other = readHeard(
      heardAnswer("req-aaaaaaaa", matchA(), { language: "Klingon" }),
      "req-aaaaaaaa",
    )!;
    expect(other.language).toBeNull();
  });

  test("nothing heard is a normal result with a reason", () => {
    for (const reason of [
      "no_speech",
      "language",
      "low_confidence",
      "empty",
      "too_short",
    ]) {
      const heard = readHeard(
        heardAnswer("req-aaaaaaaa", null, {
          heard: false,
          reason,
          transcript: "",
          language: null,
        }),
        "req-aaaaaaaa",
      )!;
      expect(heard.heard).toBe(false);
      expect(heard.reason).toBe(reason as HeardReason);
    }
    const odd = readHeard(
      heardAnswer("req-aaaaaaaa", null, {
        heard: false,
        reason: "voices",
        transcript: "",
      }),
      "req-aaaaaaaa",
    )!;
    expect(odd.reason).toBe("other");
  });

  test("something heard without a usable match, or without text, is refused", () => {
    expect(
      readHeard(heardAnswer("req-aaaaaaaa", null), "req-aaaaaaaa"),
    ).toBeNull();
    expect(
      readHeard(
        heardAnswer("req-aaaaaaaa", matchA(), { transcript: "  " }),
        "req-aaaaaaaa",
      ),
    ).toBeNull();
    expect(
      readHeard(
        heardAnswer("req-aaaaaaaa", matchA(), { heard: "yes" }),
        "req-aaaaaaaa",
      ),
    ).toBeNull();
  });
});

describe("the state machine", () => {
  test("a new recording clears the proposal, the problem and the notice", () => {
    const before = withProposal(matchA());
    expect(before.phase).toBe("proposal");
    const next = speechReducer(before, { type: "begin", seq: 2 });
    expect(next).toMatchObject({
      phase: "recording",
      seq: 2,
      opening: true,
      proposal: null,
      problem: null,
      notice: null,
    });
  });

  test("only the newest recording can become the proposal, whatever the order of the answers", () => {
    const heardA = heardOf(matchA(KNOWN[0]));
    const heardB = heardOf(matchA(KNOWN[1]));
    const setup = run([
      { type: "begin", seq: 1 },
      { type: "uploading", seq: 1 },
      { type: "begin", seq: 2 },
      { type: "uploading", seq: 2 },
    ]);
    const answer = (seq: number, heard: Heard): SpeechAction => ({
      type: "answer",
      seq,
      at: 5,
      heard,
      forModel: "m",
      truncated: false,
    });
    // B answers first, then the older A
    const bThenA = run([answer(2, heardB), answer(1, heardA)], setup);
    expect(bThenA.proposal?.match.best?.prompt).toBe(KNOWN[1]);
    // A answers first (stale, dropped), then B
    const aThenB = run([answer(1, heardA), answer(2, heardB)], setup);
    expect(aThenB.proposal?.match.best?.prompt).toBe(KNOWN[1]);
    expect(aThenB.proposal?.id).toBe(2);
  });

  test("a stale failure does not replace a newer recording either", () => {
    const state = run([
      { type: "begin", seq: 1 },
      { type: "uploading", seq: 1 },
      { type: "begin", seq: 2 },
      { type: "failed", seq: 1, problem: { kind: "format" } },
    ]);
    expect(state.phase).toBe("recording");
    expect(state.problem).toBeNull();
  });

  test("nothing heard ends in idle with the reason, not in a proposal", () => {
    const quiet = readHeard(
      heardAnswer("req-12345678", null, {
        heard: false,
        reason: "no_speech",
        transcript: "",
      }),
      "req-12345678",
    )!;
    const state = run([
      { type: "begin", seq: 1 },
      { type: "uploading", seq: 1 },
      {
        type: "answer",
        seq: 1,
        at: 0,
        heard: quiet,
        forModel: "m",
        truncated: false,
      },
    ]);
    expect(state).toMatchObject({
      phase: "idle",
      proposal: null,
      notice: { kind: "heard", reason: "no_speech" },
    });
  });

  test("the proposal expires, but only the one that was current", () => {
    const state = withProposal(matchA(), 0, 4);
    expect(speechReducer(state, { type: "expire", id: 3 })).toBe(state);
    expect(speechReducer(state, { type: "expire", id: 4 })).toMatchObject({
      phase: "idle",
      proposal: null,
      notice: { kind: "expired" },
    });
  });

  test("Use is armed once, for the proposal that asked", () => {
    const state = withProposal(matchA(), 0, 4);
    expect(state.proposal?.armed).toBe(false);
    expect(speechReducer(state, { type: "arm", id: 3 }).proposal?.armed).toBe(
      false,
    );
    expect(speechReducer(state, { type: "arm", id: 4 }).proposal?.armed).toBe(
      true,
    );
  });

  test("checking again freezes the proposal until the new answer, which is a new proposal", () => {
    const armed = speechReducer(withProposal(matchA(), 0, 1), {
      type: "arm",
      id: 1,
    });
    const checking = speechReducer(armed, { type: "check", seq: 2 });
    expect(checking.proposal).toMatchObject({ checking: true, armed: false });
    expect(
      speechReducer(checking, { type: "arm", id: 1 }).proposal?.armed,
    ).toBe(false);
    const done = speechReducer(checking, {
      type: "checked",
      seq: 2,
      at: 900,
      transcript: "x",
      match: match(matchC()),
      forModel: "m2",
      edited: true,
    });
    expect(done.proposal).toMatchObject({
      id: 2,
      edited: true,
      language: null,
      armed: false,
      checking: false,
      forModel: "m2",
    });
    // an answer for an older check is dropped
    const stale = speechReducer(checking, {
      type: "checked",
      seq: 1,
      at: 900,
      transcript: "x",
      match: match(matchC()),
      forModel: "m2",
      edited: true,
    });
    expect(stale.proposal?.checking).toBe(true);
  });

  test("a revalidation keeps the language and the model that heard the text", () => {
    const state = speechReducer(withProposal(matchA(), 0, 1), {
      type: "check",
      seq: 2,
    });
    const done = speechReducer(state, {
      type: "checked",
      seq: 2,
      at: 5,
      transcript: "t",
      match: match(matchC()),
      forModel: "m2",
      edited: false,
    });
    expect(done.proposal).toMatchObject({
      language: "English",
      model: "qwen3-asr-1.7b-hf",
    });
  });

  test("picking works only among what is on offer", () => {
    const state = withProposal(matchB());
    expect(
      speechReducer(state, { type: "pick", prompt: "rm -rf /" }).proposal
        ?.picked,
    ).toBeNull();
    expect(
      speechReducer(state, { type: "pick", prompt: KNOWN[0] }).proposal?.picked,
    ).toBe(KNOWN[0]);
    const all = speechReducer(
      speechReducer(state, { type: "pick", prompt: KNOWN[0] }),
      { type: "show_all" },
    );
    expect(all.proposal).toMatchObject({ showAll: true, picked: null });
  });

  test("editing starts from the transcript and a mic problem ends a proposal", () => {
    const state = speechReducer(withProposal(matchA()), {
      type: "edit",
      on: true,
    });
    expect(state.proposal).toMatchObject({
      editing: true,
      draft: "pick the eggplant in the blue plate",
    });
    expect(
      speechReducer(state, { type: "draft", text: "pick" }).proposal?.draft,
    ).toBe("pick");
    expect(
      speechReducer(state, { type: "mic_problem", code: "denied" }),
    ).toMatchObject({
      phase: "error",
      proposal: null,
      problem: { kind: "mic", code: "denied" },
    });
  });

  test("Use leaves a notice and no proposal", () => {
    expect(
      speechReducer(withProposal(matchA()), { type: "used" }),
    ).toMatchObject({
      phase: "idle",
      proposal: null,
      notice: { kind: "used" },
    });
  });
});

describe("canUse", () => {
  const armed = (raw: Record<string, unknown>, at = 1000) =>
    speechReducer(withProposal(raw, at), { type: "arm", id: 1 });

  test("is false until the proposal has been on screen for 800 ms, even if the state says armed", () => {
    const state = armed(matchA());
    expect(canUse(state, KNOWN[2], 1000)).toBe(false);
    expect(canUse(state, KNOWN[2], 1000 + 799)).toBe(false);
    expect(canUse(state, KNOWN[2], 1000 + 800)).toBe(true);
  });

  test("is false before the arm and after the 60 s", () => {
    expect(canUse(withProposal(matchA()), KNOWN[2], 5000)).toBe(false);
    const state = armed(matchA());
    expect(canUse(state, KNOWN[2], 1000 + 60_000 - 1)).toBe(true);
    expect(canUse(state, KNOWN[2], 1000 + 60_000)).toBe(false);
  });

  test("tier A hands over its sentence and no other", () => {
    const state = armed(matchA(KNOWN[2]));
    expect(canUse(state, KNOWN[2], 3000)).toBe(true);
    expect(canUse(state, KNOWN[0], 3000)).toBe(false);
    expect(
      canUse(state, "pick the eggplant in the blue plate; rm -rf /", 3000),
    ).toBe(false);
  });

  test("tier B offers three sentences, and after 'None of these' the whole list", () => {
    const state = armed(matchB(), 0);
    const two = matchB({
      alternatives: [candidate(KNOWN[0])],
      allowed: [...KNOWN, "a fourth allowed one"],
    });
    expect(visiblePrompts(state.proposal!)).toEqual([
      KNOWN[1],
      KNOWN[0],
      KNOWN[2],
    ]);
    const fourth = armed(two, 0);
    expect(canUse(fourth, "a fourth allowed one", 3000)).toBe(false);
    const all = speechReducer(fourth, { type: "show_all" });
    expect(canUse(all, "a fourth allowed one", 3000)).toBe(true);
  });

  test("tier C offers the whole allowed list; a flag offers nothing", () => {
    expect(visiblePrompts(armed(matchC(), 0).proposal!)).toEqual(KNOWN);
    for (const flag of ["stop", "negation", "unsafe"]) {
      const state = armed(matchV(flag), 0);
      expect(visiblePrompts(state.proposal!)).toEqual([]);
      for (const prompt of KNOWN)
        expect(canUse(state, prompt, 5000)).toBe(false);
    }
  });

  test("a flag on a result of another tier still offers nothing", () => {
    const state = armed(matchA(KNOWN[0], { flag: "negation" }), 0);
    expect(canUse(state, KNOWN[0], 5000)).toBe(false);
  });

  test("is false while the proposal is being checked again, and with no proposal", () => {
    const state = speechReducer(armed(matchA(), 0), { type: "check", seq: 2 });
    expect(canUse(state, KNOWN[2], 5000)).toBe(false);
    expect(canUse(initialSpeech, KNOWN[2], 5000)).toBe(false);
  });
});

describe("availability (fail closed)", () => {
  const ok = (over: Record<string, unknown> = {}): StatusRead => ({
    kind: "ok",
    status: readSpeechStatus(statusAnswer(over))!,
  });
  const keys = (read: StatusRead) =>
    availability(read).reasons.map((r) =>
      r.say && "key" in r.say ? r.say.key : r.text,
    );

  test("is off while the state is not known and when it cannot be read", () => {
    expect(availability({ kind: "loading" })).toMatchObject({ usable: false });
    const failure = errorOf(404, { detail: "Not Found" });
    const lost = availability({ kind: "problem", failure });
    expect(lost.usable).toBe(false);
    expect(keys({ kind: "problem", failure })[0]).toBe(
      "marvel0.speech.status_failed",
    );
    expect(availability({ kind: "problem", failure: null }).usable).toBe(false);
  });

  test("is on when installed, not unavailable and the gate allows", () => {
    for (const state of ["off", "loading", "ready", "busy", "failed"])
      expect(availability(ok({ state })).usable).toBe(true);
  });

  test("says how to install when the worker or the model is missing", () => {
    expect(
      availability(ok({ installed: { worker: false, model: true } })),
    ).toMatchObject({ usable: false, install: true });
    expect(
      availability(ok({ installed: { worker: true, model: false } })),
    ).toMatchObject({ usable: false, install: true });
  });

  test("an unavailable state gives the server's reason in words when the code is known", () => {
    const read = ok({
      state: "unavailable",
      reason: { code: "speech_not_installed", text: "No worker" },
    });
    expect(availability(read).usable).toBe(false);
    expect(keys(read)).toEqual(["marvel0.err.speech.not_installed"]);
    expect(keys(ok({ state: "unavailable" }))).toEqual([
      "marvel0.speech.unavailable",
    ]);
    const odd = ok({
      state: "unavailable",
      reason: { code: "weird", text: "Something odd" },
    });
    expect(keys(odd)).toEqual(["Something odd"]);
  });

  test("a closed gate names each reason in words", () => {
    const read = ok({
      gate: { allowed: false, reasons: [...GATE_REASONS, "unheard_of"] },
    });
    expect(availability(read).usable).toBe(false);
    expect(keys(read)).toEqual([
      ...GATE_REASONS.map((r) => `marvel0.speech.gate.${r}`),
      "unheard_of",
    ]);
    expect(keys(ok({ gate: { allowed: false, reasons: [] } }))).toEqual([
      "marvel0.speech.gate.generic",
    ]);
  });
});

describe("words", () => {
  const PARAMS = {
    n: 2,
    said: "S",
    expected: "E",
    heard: "H",
    as: "A",
    make: "M",
    model: "mod",
    language: "L",
  };
  const holes = (text: string) =>
    [...text.matchAll(/\{(\w+)\}/g)].map((m) => m[1]);
  const keysOf = (say: Say): string[] => ("key" in say ? [say.key] : []);

  test("every code, reason, state and difference has words in both languages, with the same placeholders", () => {
    const all: Say[] = [
      ...SPEECH_CODES.map(
        (code) =>
          failureWords(errorOf(409, { detail: { code, message: "m" } })).say!,
      ),
      ...GATE_REASONS.map((r) => ({ key: `marvel0.speech.gate.${r}` })),
      ...DIFFERENCE_CODES.map(
        (code) =>
          describeCandidate(
            match(
              matchA(KNOWN[0], {
                best: candidate(KNOWN[0], {
                  differences: [{ code, said: "s", expected: "e" }],
                }),
              }),
            ).best!,
          )[0],
      ),
      ...(
        ["unavailable", "off", "loading", "ready", "busy", "failed"] as const
      ).map(engineSay),
      ...(
        [
          "no_speech",
          "language",
          "low_confidence",
          "empty",
          "too_short",
          "other",
        ] as const
      ).map(heardSay),
      ...["English", "Chinese", null].map(languageSay),
      ...(
        [
          "insecure_context",
          "unsupported",
          "denied",
          "no_device",
          "busy",
          "not_ready",
          "too_short",
          "silent",
          "audio_graph",
          "disconnected",
          "encode",
          "error",
        ] as const
      ).map(micSay),
    ];
    expect(all.length).toBeGreaterThan(40);
    for (const say of all)
      for (const key of keysOf(say)) {
        expect(key in en, key).toBe(true);
        expect(key in zh, key).toBe(true);
        expect(holes(en[key as keyof typeof en]).sort(), key).toEqual(
          holes(zh[key as keyof typeof zh]).sort(),
        );
        const text = sayText({ key, params: PARAMS }, catalogue("zh"));
        expect(text, key).not.toMatch(/\{\w+\}/);
      }
  });

  test("a code of the voice routes is worded from the catalogue; a shared one by marvel0-failure; an unknown one as written", () => {
    expect(
      failureWords(
        errorOf(429, { detail: { code: "speech_busy", message: "x" } }),
      ).say,
    ).toEqual({ key: "marvel0.err.speech.busy" });
    expect(
      failureWords(errorOf(401, { detail: { code: "ui_token", message: "x" } }))
        .say,
    ).toEqual({ key: "marvel0.err.ui_token" });
    expect(
      failureWords(
        errorOf(400, { detail: { code: "request_id_invalid", message: "x" } }),
      ).say,
    ).toEqual({ key: "marvel0.err.request_id_invalid" });
    const odd = failureWords(
      errorOf(418, { detail: { code: "teapot", message: "I am a teapot" } }),
    );
    expect(odd.say).toBeNull();
    expect(odd.text).toBe("I am a teapot");
  });

  test("the server's reason for an unavailable state is worded with or without the speech_ prefix", () => {
    expect(reasonWords({ code: "not_installed", text: "t" })?.say).toEqual({
      key: "marvel0.err.speech.not_installed",
    });
    expect(
      reasonWords({ code: "speech_model_missing", text: "t" })?.say,
    ).toEqual({ key: "marvel0.err.speech.model_missing" });
    expect(reasonWords(null)).toBeNull();
  });

  test("fixes come first, then differences by code; an unknown code has a general sentence", () => {
    const lines = describeCandidate({
      prompt: KNOWN[0],
      score: 90,
      fixes: [{ heard: "egplant", as: "eggplant" }],
      differences: [
        { code: "color_mismatch", said: "red", expected: "blue" },
        { code: "never_seen", said: "", expected: "" },
      ],
    });
    expect(lines.map((line) => ("key" in line ? line.key : ""))).toEqual([
      "marvel0.speech.fix",
      "marvel0.speech.diff.color_mismatch",
      "marvel0.speech.diff.other",
    ]);
    expect(words(lines[0])).toBe("Read “egplant” as “eggplant”.");
    expect(words(lines[1])).toBe(
      "Colour: you said “red”, the instruction says “blue”.",
    );
    expect(words(lines[1], "zh")).toBe("颜色：你说的是“red”，指令里是“blue”。");
    expect(words(lines[2])).toContain("“?”");
    expect(candidateOf(match(matchB()), KNOWN[1])?.differences).toHaveLength(1);
  });

  test("sayParts sets the values apart from the words around them", () => {
    const parts = sayParts(
      "You said “{said}” and {extra}, not {nested}.",
      { said: "red⁧", nested: { key: "k" } },
      () => "NESTED",
    );
    expect(parts).toEqual([
      { text: "You said “", value: false },
      { text: "red⁧", value: true },
      { text: "” and {extra}, not ", value: false },
      { text: "NESTED", value: false },
      { text: ".", value: false },
    ]);
  });
});

test("allowed task strings retain exact whitespace and reject invisible values", () => {
  const exact = "pick  the eggplant in the blue plate";
  const result = readMatch(matchA(exact, { allowed: [exact] }));
  expect(result?.best?.prompt).toBe(exact);
  expect(result?.allowed).toEqual([exact]);
  expect(readMatch(matchA(KNOWN[2], { allowed: [exact] }))).toBeNull();
  for (const bad of [
    "pick\tthe eggplant",
    "pick" + String.fromCharCode(0x200b) + " the eggplant",
    " " + KNOWN[2],
  ]) {
    expect(readMatch(matchA(bad, { allowed: [bad] }))).toBeNull();
  }
});

describe("the generated sentence (owner decision 2026-10-30)", () => {
  const armed = (raw: Record<string, unknown>, at = 1000) =>
    speechReducer(withProposal(raw, at), { type: "arm", id: 1 });
  const GEN = "pick eggplant into blue bowl";
  const ORIGINAL = "put eggplant into blue bowl";

  test("is read with its slots, and only an ok status carries a sentence", () => {
    const match = readMatch(matchC({ generated: generated() }))!;
    expect(match.generated).toMatchObject({
      status: "ok",
      prompt: GEN,
      datasetOriginal: ORIGINAL,
      episodes: 117,
    });
    expect(match.generated!.slots).toHaveLength(2);
    const none = readMatch(
      matchC({ generated: generated({ status: "untranslated" }) }),
    )!;
    expect(none.generated).toMatchObject({
      status: "untranslated",
      prompt: null,
      datasetOriginal: null,
    });
  });

  test("an unknown status, or an ok without a clean sentence, is dropped", () => {
    for (const bad of [
      generated({ status: "maybe" }),
      generated({ prompt: " pick eggplant " }),
      generated({ prompt: "pick\u0000eggplant" }),
      generated({ prompt: 3 }),
      "ok",
      null,
    ])
      expect(readMatch(matchC({ generated: bad }))!.generated).toBeNull();
    expect(readMatch(matchC())!.generated).toBeNull();
  });

  test("Use takes the generated sentence and its original, and nothing else outside the list", () => {
    const state = armed(matchC({ generated: generated() }));
    expect(canUse(state, GEN, 1800)).toBe(true);
    expect(canUse(state, ORIGINAL, 1800)).toBe(true);
    expect(canUse(state, "pick eggplant into red bowl", 1800)).toBe(false);
    expect(canUse(state, GEN, 1799)).toBe(false);
  });

  test("never for a flagged result or the restricted family", () => {
    expect(
      generatedPrompts(readMatch(matchV("stop", { generated: generated() }))!),
    ).toEqual([]);
    const restricted = readMatch(
      matchC({
        family: "restricted",
        allowed: RESTRICTED,
        generated: generated(),
      }),
    )!;
    expect(generatedPrompts(restricted)).toEqual([]);
    expect(
      canUse(
        armed(
          matchC({
            family: "restricted",
            allowed: RESTRICTED,
            generated: generated(),
          }),
        ),
        GEN,
        1800,
      ),
    ).toBe(false);
  });
});
