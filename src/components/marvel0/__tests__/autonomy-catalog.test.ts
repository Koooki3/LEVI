import { describe, expect, test } from "bun:test";
import { readFileSync } from "fs";
import { join } from "path";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import {
  KEY_IDS,
  MODES,
  NOTICE_CODES,
  READY_CODES,
  READY_LEVELS,
  STATE_IDS,
  readyWordCode,
  STOP_PHASES,
} from "../autonomy-doc";
import {
  exitKey,
  modeHintKey,
  modeKey,
  stateHintKey,
  stateKey,
} from "../autonomy-logic";
import {
  CODES,
  READY_VIEWS,
  RUN_VIEWS,
  readyDoc,
  runDoc,
} from "./autonomy-fixtures";

// The words of Step 5: everything the backend can send by code has a sentence
// in both languages (a code with none would show the server's English in a
// Chinese page), no sentence is left without a code, and the Chinese follows
// the rules of the other cards of the page.

const E = en as Record<string, string>;
const Z = zh as Record<string, string>;
const P = "marvel0.autonomy.";
const root = join(import.meta.dir, "../../../..");
const state = readFileSync(join(root, "levi/marvel0/aeri_state.py"), "utf8");
const ready = readFileSync(join(root, "levi/marvel0/aeri_ready.py"), "utf8");
const family = (prefix: string) =>
  Object.keys(E)
    .filter((key) => key.startsWith(P + prefix))
    .map((key) => key.slice((P + prefix).length));
const tupleOf = (name: string): string[] => {
  const body = state.match(
    new RegExp(`\\n${name} = \\(([\\s\\S]*?)\\n\\)`),
  )![1];
  return [...body.matchAll(/"(\w+)"/g)].map((m) => m[1]);
};
const refusalCodes = (): string[] => {
  const body = state.match(/\nREFUSALS = \{([\s\S]*?)\n\}\n/)![1];
  return [...body.matchAll(/^    "(\w+)": \(/gm)].map((m) => m[1]);
};
const both = (key: string) => key in E && key in Z;
const missing = (keys: string[]) => keys.filter((key) => !both(key));
const sameSet = (a: readonly string[], b: readonly string[]) =>
  expect([...new Set(a)].sort()).toEqual([...new Set(b)].sort());
/** A code with a sentence in this step's family or in the evaluation client's
 * (the checks the two share), in both languages. */
const errWorded = (code: string) =>
  [
    `${P}err.${code}`,
    `marvel0.run.refuse.${code}`,
    `marvel0.run.why.${code}`,
  ].some(both);

describe("what the backend names by code is worded", () => {
  test("the states, each with its word and its sentence; nothing is left over", () => {
    expect(missing(STATE_IDS.map(stateKey))).toEqual([]);
    // A sentence for each state, and for each state after each stop phase
    // (the states that follow a Stop say what the Stop has reached).
    const hints = [
      ...STATE_IDS.flatMap((id) =>
        STOP_PHASES.map((phase) => stateHintKey(id, phase)),
      ),
      // ... and the one reason that changes what an unreadable D means: another
      // core of LEVI owns the run.
      stateHintKey("unknown", "idle", "no_keyboard", false),
      // ... and a D alive that the page cannot follow, whose Stop is offered.
      stateHintKey("unknown", "idle", "no_keyboard", true),
      // ... and the Stop that D did not answer when the backend offers no
      // retry (it must not promise "one more try").
      stateHintKey("stopping", "unconfirmed", null, false),
    ];
    expect(missing(hints)).toEqual([]);
    sameSet(family("state."), STATE_IDS);
    sameSet(
      family("hint."),
      hints.map((key) => key.slice(`${P}hint.`.length)),
    );
    sameSet(tupleOf("STATES"), STATE_IDS);
  });

  test("the keys: a label for every action; the two wordings of the resume key; nothing is left over", () => {
    sameSet(
      family("key.").filter((id) => id !== "resume_ask_confirm"),
      KEY_IDS,
    );
    expect(both(`${P}key.resume_ask_confirm`)).toBe(true);
    sameSet(tupleOf("ACTIONS"), KEY_IDS);
  });

  test("every reason of a state (the backend's REASONS) has a sentence; nothing is left over", () => {
    const reasons = tupleOf("REASONS");
    expect(reasons.length).toBeGreaterThan(30);
    expect(missing(reasons.map((code) => `${P}reason.${code}`))).toEqual([]);
    sameSet(family("reason."), reasons);
  });

  test("every reason of every sample is one of them", () => {
    const reasons = new Set(tupleOf("REASONS"));
    for (const name of RUN_VIEWS) {
      const reason = runDoc(name).state.reason as string;
      expect([name, reasons.has(reason)]).toEqual([name, true]);
    }
  });

  test("every refusal and every reason a key is off (REFUSALS, why_not, the codes file) has a sentence", () => {
    const codes = [
      ...refusalCodes(),
      ...tupleOf("WHY_NOT"),
      ...CODES.why_not,
      ...Object.keys(CODES.errors),
    ];
    expect(codes.length).toBeGreaterThan(40);
    expect([...new Set(codes)].filter((code) => !errWorded(code))).toEqual([]);
  });

  test("the refusals of the preparation card, of the live service and of the leftovers", () => {
    const sample = JSON.parse(
      readFileSync(join(import.meta.dir, "marvel0-aeri.samples.json"), "utf8"),
    );
    const codes = [
      ...sample.codes.errors,
      ...sample.codes.why_not,
      ...Object.keys(sample.prepare.errors),
      ...Object.keys(sample.live_stop.errors ?? {}),
      ...Object.keys(sample.leftovers_stop.errors ?? {}),
    ];
    expect(codes.length).toBeGreaterThan(20);
    expect(
      [...new Set<string>(codes)].filter((code) => !errWorded(code)),
    ).toEqual([]);
  });

  test("the notices of the run document", () => {
    sameSet(tupleOf("NOTICES"), NOTICE_CODES);
    sameSet(family("notice."), NOTICE_CODES);
    expect(missing(NOTICE_CODES.map((code) => `${P}notice.${code}`))).toEqual(
      [],
    );
  });

  test("the stop phases: a sentence for each the card can show", () => {
    sameSet(STOP_PHASES, CODES.stop_phases);
    for (const key of ["ready", "sent", "acked", "retry", "spent"])
      expect(both(`${P}stop.${key}`)).toBe(true);
  });

  test("the Stop frame when no Stop is offered: a sentence for the frame, for D's silence, and for each reason the review named", () => {
    for (const key of [
      "off",
      "unoffered",
      "unoffered_tail",
      "why.stop_spent",
      "why.driven_elsewhere",
      "why.d_gone",
    ])
      expect(both(`${P}stop.${key}`)).toBe(true);
    // The three codes are the ones the backend gives as `stop.why_not`.
    for (const code of ["stop_spent", "driven_elsewhere", "d_gone"])
      expect(CODES.why_not).toContain(code);
    // No sentence of the silent Stop promises a retry; the one that is offered does.
    expect(E[`${P}stop.unoffered`]).not.toMatch(/more try|last/i);
    expect(E[`${P}stop.retry`]).toMatch(/One more try is offered/);
    expect(Z[`${P}stop.unoffered`]).not.toContain("再试");
    // The way out that works without this page is named with its exact name.
    expect(E[`${P}stop.unoffered_tail`]).toContain(
      "scripts/aeri_page_stop.py --again",
    );
  });

  test("the notes of the switch turned off in the control file while D runs, and of the release tick, the screen quote and the page's own refusals", () => {
    for (const key of [
      "disabled.live",
      "local.switched_off",
      "dcheck.scene_ready",
      "confirm.checks_release",
      "confirm.screen",
      "err.attest_incomplete",
    ])
      expect(both(`${P}${key}`)).toBe(true);
    // The sentence for a live D does not say that nothing on the step acts:
    // Stop and Force-end do.
    expect(E[`${P}disabled.live`]).not.toMatch(/nothing on this step acts/);
    expect(E[`${P}disabled.live`]).toMatch(/Stop and Force-end still work/);
    expect(Z[`${P}disabled.live`]).toContain("仍然可用");
  });

  test("the note that replaces Step 5 when it cannot load names the exact command, what it needs and the physical e-stop", () => {
    for (const key of [
      "load_failed",
      "load_failed_stop",
      "load_failed_needs",
      "load_failed_estop",
    ])
      expect(both(`${P}${key}`)).toBe(true);
    for (const text of [E, Z]) {
      expect(text[`${P}load_failed_needs`]).toContain("record.json");
      expect(text[`${P}load_failed_needs`]).toContain("--workspace DIR");
      expect(text[`${P}load_failed_needs`]).toContain("--again");
    }
    expect(E[`${P}load_failed_estop`]).toMatch(/physical e-stop/);
    expect(Z[`${P}load_failed_estop`]).toContain("物理急停");
  });

  test("the readiness rows: a sentence for each code the backend lists, and the levels", () => {
    const codes = (CODES as { readiness?: string[] }).readiness;
    const fromBackend = [...ready.matchAll(/_row\(\s*"(\w+)"/g)].map(
      (m) => m[1],
    );
    const sampled = new Set<string>();
    for (const name of READY_VIEWS)
      for (const row of readyDoc(name).readiness) sampled.add(row.code);
    expect(sampled.size).toBeGreaterThanOrEqual(READY_CODES.length - 2);
    for (const code of [
      ...sampled,
      ...READY_CODES,
      ...(codes ?? []),
      ...fromBackend,
    ])
      expect([code, both(`${P}ready.${code}`)]).toEqual([code, true]);
    sameSet(
      family("ready.").filter(
        (c) =>
          /^[a-z_]+$/.test(c) &&
          ![
            "title",
            "count",
            "met",
            "empty",
            "unreadable",
            "stale",
            "paused",
          ].includes(c),
      ),
      READY_CODES,
    );
    sameSet(family("level."), READY_LEVELS);
  });

  test("the three sentences of the unit_timeout row, with the same facts in both languages; the closed code picks one by level and by what was sent", () => {
    const keys = [
      "ready.unit_timeout",
      "ready.unit_timeout.ok",
      "ready.unit_timeout.unknown",
    ];
    expect(missing(keys.map((key) => P + key))).toEqual([]);
    // The warning names the value found and the time D needs; the other two
    // need only the time (a unit that was not read has no value to name).
    expect(E[`${P}ready.unit_timeout`]).toContain("{seconds}");
    expect(E[`${P}ready.unit_timeout.ok`]).not.toContain("{seconds}");
    expect(E[`${P}ready.unit_timeout.unknown`]).not.toContain("{seconds}");
    for (const key of keys) {
      expect(E[P + key]).toContain("levi-product.service");
      expect(E[P + key]).toContain("{need_s}");
    }
    expect(E[`${P}ready.unit_timeout`]).toContain("levi stop");
    expect(E[`${P}ready.unit_timeout.unknown`]).toContain("levi stop");
    expect(readyWordCode("unit_timeout", "warn", { seconds: 15 })).toBe(
      "unit_timeout",
    );
    expect(readyWordCode("unit_timeout", "warn", { need_s: 150 })).toBe(
      "unit_timeout.unknown",
    );
    expect(readyWordCode("unit_timeout", "ok", { seconds: 180 })).toBe(
      "unit_timeout.ok",
    );
    expect(readyWordCode("stack_ready", "warn", {})).toBe("stack_ready");
    // The backend names the row and keeps it advice (never a refusal).
    expect(ready).toContain('"unit_timeout"');
    expect(ready).toMatch(/WARN_ROWS = \([^)]*"unit_timeout"/);
    expect(READY_CODES).toContain("unit_timeout");
  });

  test("modes, exits, waits, locks, the homing steps and the acks", () => {
    expect(missing(MODES.flatMap((m) => [modeKey(m), modeHintKey(m)]))).toEqual(
      [],
    );
    expect(
      missing(
        [
          "completed",
          "unfinished",
          "refused",
          "cancelled",
          "host",
          "signal",
          "other",
        ].map(exitKey),
      ),
    ).toEqual([]);
    sameSet(family("lockstate."), [
      "free",
      "held_by_d",
      "held_elsewhere",
      "d_unit",
      "other",
      "unknown",
    ]);
    sameSet(family("homing."), ["1", "2", "3"]);
    sameSet(family("ack."), [
      "estop",
      "scene_ok",
      "area_clear",
      "arm_home",
      "leftovers_checked",
    ]);
    const waits = new Set<string>();
    for (const name of RUN_VIEWS) {
      const reason = runDoc(name).wait?.reason;
      if (reason) waits.add(reason);
    }
    for (const reason of waits)
      expect([reason, both(`${P}waitreason.${reason}`)]).toEqual([
        reason,
        true,
      ]);
    // The reasons the console of AERI can wait for (levi/automatic/console.py).
    for (const reason of [
      "operator_stop",
      "contract_violation_limit",
      "policy_error",
      "home_not_executed",
      "home_pose_not_reached",
      "vlm_unavailable",
      "subtask_retry_limit",
      "camera_stale",
      "startup_home_not_executed",
    ])
      expect(both(`${P}waitreason.${reason}`)).toBe(true);
  });
});

describe("the wording rules of the Chinese text", () => {
  const ours = Object.keys(E).filter((key) => key.startsWith(P));

  test("there is a Chinese sentence for every English one, and the same placeholders", () => {
    expect(ours.length).toBeGreaterThan(300);
    for (const key of ours) {
      expect(Z[key]).toBeTruthy();
      const names = (text: string) =>
        [...text.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort();
      expect([key, names(Z[key])]).toEqual([key, names(E[key])]);
    }
  });

  test("a headline in Chinese has no Latin letter (names go in the text that follows)", () => {
    const headlines = ours.filter(
      (key) => /(\.title|_title|\.summary)$/.test(key) || key === `${P}step`,
    );
    expect(headlines.length).toBeGreaterThan(8);
    for (const key of headlines)
      expect([key, Z[key].replace(/\{\w+\}/g, "").match(/[A-Za-z]/g)]).toEqual([
        key,
        null,
      ]);
  });

  test("the state words and the buttons' labels have none either", () => {
    for (const key of ours.filter((k) => /^marvel0\.autonomy\.state\./.test(k)))
      expect([key, Z[key].replace(/\{\w+\}/g, "").match(/[A-Za-z]/g)]).toEqual([
        key,
        null,
      ]);
  });

  test("a space stands between a Chinese character and a Latin word", () => {
    for (const key of ours)
      expect([key, Z[key].match(/[一-鿿][A-Za-z]|[A-Za-z][一-鿿]/g)]).toEqual([
        key,
        null,
      ]);
  });

  test("no Chinese text is the English one, except where it is an example the policy is told in English", () => {
    for (const key of ours) {
      if (key === `${P}form.task_example`) continue;
      if (E[key].length > 12)
        expect([key, Z[key] === E[key]]).toEqual([key, false]);
    }
  });
});

describe("what the page says about what the gates are worth", () => {
  test("nothing here calls anything an emergency stop: it says that nothing is, or points at the physical one", () => {
    const allowed = [
      /not an emergency stop/gi,
      /nothing on this page is an emergency stop/gi,
      /is not an emergency stop/gi,
      /(?:a hand is |hand )?on the physical e-stop/gi,
      /physical (?:emergency stop|e-stop)/gi,
      /the e-stop/gi,
      /the physical e-stop and Franka Desk are/gi,
      /Franka Desk and the physical emergency stop are the barrier/gi,
    ];
    let checked = 0;
    for (const [key, text] of Object.entries(E)) {
      if (!key.startsWith(P) || !/emergency stop|e-stop/i.test(text)) continue;
      checked += 1;
      let rest = text;
      for (const rule of allowed) rest = rest.replace(rule, "");
      expect([key, rest.match(/emergency stop|e-stop/gi)]).toEqual([key, null]);
    }
    expect(checked).toBeGreaterThan(6);
  });

  test("the fixed sentence says what the buttons are, what Stop is and what the checks are worth, in both languages", () => {
    expect(E[`${P}honest`]).toMatch(/not an emergency stop/);
    expect(E[`${P}honest`]).toMatch(
      /stands for pressing the same key in terminal D/,
    );
    expect(E[`${P}honest`]).toMatch(
      /Stop is Ctrl\+C: the arm stays where it is and does not go home/,
    );
    expect(E[`${P}honest`]).toMatch(
      /cannot stop another program of the same account/,
    );
    expect(Z[`${P}honest`]).toContain("这不是急停");
    expect(Z[`${P}honest`]).toContain("停止等于 Ctrl+C");
    expect(Z[`${P}honest`]).toContain("防不了同一账号下的其他程序");
  });

  test("a key's hazard is said in the page's words for every key that moves the arm or may", () => {
    for (const id of [
      "execute",
      "release_round",
      "home_h",
      "verdict_success",
      "verdict_discard",
      "resume_ask",
      "resume_confirm",
    ])
      expect(
        both(
          `${P}hazard.${id === "verdict_success" ? "verdict" : id === "verdict_discard" ? "discard" : id}`,
        ),
      ).toBe(true);
  });
});
