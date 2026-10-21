import { describe, expect, test } from "bun:test";
import { readFileSync } from "fs";
import { join } from "path";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import { NODE_LABEL } from "../chain-logic";
import {
  COMP_STATES,
  SEQUENCE_STATES,
  SEQUENCE_STEP_STATES,
} from "../stack-act-logic";
import { RUN_STATES, ACTIONS } from "../run-logic";

// The words the control page uses for what the backend sends by code. A code
// the backend can send that the catalogue cannot word fails here (the page
// would show the server's English sentence in a Chinese page), and so does a
// catalogue entry whose code the backend no longer has (a dead sentence).

const root = join(import.meta.dir, "../../../..");
const backend = (file: string) =>
  readFileSync(join(root, "levi/marvel0", file), "utf8");
const act = backend("stack_act.py");
const stack = backend("stack.py");
const units = backend("stack_units.py");
const run = backend("run.py");

const E = en as Record<string, string>;
const Z = zh as Record<string, string>;
const family = (prefix: string) =>
  Object.keys(E)
    .filter((key) => key.startsWith(prefix))
    .map((key) => key.slice(prefix.length));

/** The keys of a top-level dict literal `NAME = {` ... `}` of a python file. */
function dictKeys(source: string, name: string): string[] {
  const start = source.indexOf(`\n${name} = {`);
  if (start < 0) throw new Error(`no ${name}`);
  const end = source.indexOf("\n}\n", start);
  return [...source.slice(start, end).matchAll(/^ {4}"([a-z_.]+)":/gm)].map(
    (m) => m[1],
  );
}
const sameSet = (a: string[], b: string[]) =>
  expect([...new Set(a)].sort()).toEqual([...new Set(b)].sort());
const missing = (prefix: string, codes: string[]) =>
  codes.filter((c) => !(`${prefix}${c}` in E) || !(`${prefix}${c}` in Z));

describe("the arm stack's blockers and refusals", () => {
  const gateCodes = [...stack.matchAll(/_check\(\s*"(gate\.[a-z_]+)"/g)]
    .map((m) => m[1])
    .filter((code) => !code.startsWith("gate.person_"));
  const textCodes = dictKeys(act, "TEXT");
  const stackRefusals = dictKeys(stack, "REFUSALS");

  test("every code of the backend's TEXT table and every gate has a sentence of the controls' own", () => {
    expect(textCodes.length).toBeGreaterThan(30);
    expect(gateCodes.length).toBeGreaterThan(15);
    expect(missing("marvel0.ctl.block.", [...textCodes, ...gateCodes])).toEqual(
      [],
    );
  });

  test("a code of the monitor or the marker (stack.py) has a sentence in the S4-0 family or the controls'", () => {
    const worded = (code: string) =>
      `marvel0.ctl.block.${code}` in E || `marvel0.stack.refuse.${code}` in E;
    expect(stackRefusals.filter((code) => !worded(code))).toEqual([]);
  });

  test("no sentence is left over: every entry has a code in the backend", () => {
    const known = new Set([...textCodes, ...gateCodes, ...stackRefusals]);
    expect(
      family("marvel0.ctl.block.").filter((code) => !known.has(code)),
    ).toEqual([]);
  });
});

describe("states, reasons, failures and ready lines", () => {
  test("the states of a terminal, of the guided start, of the marker and of the chain", () => {
    for (const state of COMP_STATES)
      expect(E[`marvel0.ctl.state.${state}`]).toBeTruthy();
    sameSet(family("marvel0.ctl.state."), [...COMP_STATES]);
    sameSet(family("marvel0.ctl.seq.state."), [...SEQUENCE_STATES]);
    // The guided stop words the same states, and each step's own.
    sameSet(family("marvel0.ctl.seq.stop_state."), [...SEQUENCE_STATES]);
    sameSet(family("marvel0.ctl.seq.step_state."), [...SEQUENCE_STEP_STATES]);
    sameSet(
      [
        ...act
          .match(/\nSEQUENCE_STEP_STATES = \(([^)]*)\)/s)![1]
          .matchAll(/"([a-z_]+)"/g),
      ].map((m) => m[1]),
      [...SEQUENCE_STEP_STATES],
    );
    sameSet(family("marvel0.ctl.marker.state."), [
      "present",
      "absent",
      "unknown",
    ]);
    sameSet(family("marvel0.chain.state."), [
      "ready",
      // B after a gripper test that failed: ready, with a warning.
      "ready_test_failed",
      "working",
      "idle",
      "problem",
      "attention",
      "stale",
      "unknown",
    ]);
    for (const label of Object.values(NODE_LABEL))
      expect(label in E && label in Z).toBe(true);
    // The backend has exactly these states.
    sameSet(
      [
        ...act.match(/\nSTATES = \(([^)]*)\)/s)![1].matchAll(/"([a-z_]+)"/g),
      ].map((m) => m[1]),
      [...COMP_STATES],
    );
  });

  test("the reasons a terminal gives are worded", () => {
    const suffixes = [...act.matchAll(/f"\{key\}\.([a-z_]+)"/g)].map(
      (m) => m[1],
    );
    expect(suffixes.length).toBeGreaterThan(5);
    for (const term of ["a", "b", "c"])
      expect(
        missing(
          `marvel0.ctl.reason.${term}.`,
          // Only A's sockets linger after a stop: its reason is checked below.
          suffixes.filter((s) => s !== "stopped" && s !== "sockets_draining"),
        ),
      ).toEqual([]);
    for (const code of [
      ...act.matchAll(/"(b\.[a-z_]+|a\.sockets_draining)"/g),
    ].map((m) => m[1]))
      expect(`marvel0.ctl.reason.${code}` in E).toBe(true);
    // Every entry is a suffix or a code the backend writes.
    for (const key of family("marvel0.ctl.reason.")) {
      const [term, ...rest] = key.split(".");
      expect(["a", "b", "c"]).toContain(term);
      expect(act).toContain(rest.join("."));
    }
  });

  test("failures and ready lines are the backend's rule keys", () => {
    const rules = [...units.matchAll(/\brule\(\s*(?:f)?"([a-z_{}]+)"/g)].map(
      (m) => m[1],
    );
    const controllers = [
      ...units
        .match(/GRIPPER_CONTROLLERS = \(([^)]*)\)/s)![1]
        .matchAll(/"([a-z_]+)"/g),
    ].map((m) => m[1]);
    const keys = rules.flatMap((key) =>
      key === "controller_{name}"
        ? controllers.map((c) => `controller_${c}`)
        : [key],
    );
    const extra = [
      "health_hardware_active",
      "health_controller_active",
      "listening",
    ];
    expect(keys.length).toBeGreaterThan(10);
    // Each rule key is a ready line (shown while it starts) or a failure (shown after).
    for (const key of keys)
      expect(
        `marvel0.ctl.ready.${key}` in E || `marvel0.ctl.failure.${key}` in E,
      ).toBe(true);
    for (const key of extra) expect(`marvel0.ctl.ready.${key}` in E).toBe(true);
    for (const key of ["unit_failed", "duplicate_text", "ready_timeout"])
      expect(`marvel0.ctl.failure.${key}` in E).toBe(true);
    for (const key of [
      ...family("marvel0.ctl.ready."),
      ...family("marvel0.ctl.failure."),
    ]) {
      if (["seen", "missing", "progress", "waited", "waited_of"].includes(key))
        continue;
      expect([
        ...keys,
        ...extra,
        "unit_failed",
        "duplicate_text",
        "ready_timeout",
      ]).toContain(key);
    }
  });

  test("the notes after a stop, the notices, the operations, their codes and the checks", () => {
    sameSet(
      family("marvel0.ctl.stop_note."),
      [...act.matchAll(/"([a-z]\.stop_[a-z_]+)"/g)]
        .map((m) => m[1])
        .filter((c, i, a) => a.indexOf(c) === i),
    );
    sameSet(
      family("marvel0.ctl.notice."),
      [...act.matchAll(/"notice\.([a-z_]+)"/g)].map((m) => m[1]),
    );
    for (const kind of [
      "gripper",
      "preflight_a",
      "preflight_c",
      "stop_a",
      "stop_b",
      "stop_c",
    ])
      expect(`marvel0.ctl.op.${kind}` in E).toBe(true);
    for (const code of [
      "stop_failed",
      "c_preflight_failed",
      "fci_closed",
      "fci_unreachable",
    ])
      expect(`marvel0.ctl.opcode.${code}` in E).toBe(true);
    for (const code of [
      ...readFileSync(join(root, "levi/marvel0/gripper.py"), "utf8").matchAll(
        /"code": "([a-z_]+)"/g,
      ),
    ].map((m) => m[1]))
      expect(`marvel0.ctl.gripper.code.${code}` in E).toBe(true);
    sameSet(
      family("marvel0.ctl.attest."),
      [
        ...act.match(/ATTEST_KEYS = \(([^)]*)\)/s)![1].matchAll(/"([a-z_]+)"/g),
      ].map((m) => m[1]),
    );
  });

  test("the guided start's steps and reasons", () => {
    const steps = [...act.matchAll(/def _seq_([a-z_]+)\(self, seq, ctx/g)].map(
      (m) => m[1],
    );
    // The guided stop's steps are `stop_<terminal>` in the backend's order.
    const order = [
      ...act.match(/\nSTOP_ORDER = \(([^)]*)\)/s)![1].matchAll(/"([a-z])"/g),
    ].map((m) => m[1]);
    expect(order).toEqual(["c", "a", "b"]);
    sameSet(family("marvel0.ctl.seq.step."), [
      ...steps,
      ...order.map((key) => `stop_${key}`),
    ]);
    for (const reason of [
      "cancelled",
      "expired",
      "superseded",
      "step_timeout",
      "heavy_job_still_running",
    ])
      expect(`marvel0.ctl.seq.reason.${reason}` in E).toBe(true);
    // Every reason a guided start or stop can end with has its sentence, in
    // both languages (its own family, or the refusals'): the page never shows a
    // bare code, and no sentence is left over for a reason the backend lost.
    const reasons = [
      ...act
        .match(/\nSEQUENCE_REASONS = \(([^)]*)\)/s)![1]
        .matchAll(/"([a-z_]+)"/g),
    ].map((m) => m[1]);
    expect(reasons.length).toBeGreaterThan(20);
    for (const reason of reasons)
      expect([
        reason,
        ["marvel0.ctl.seq.reason.", "marvel0.ctl.block."].some(
          (prefix) => `${prefix}${reason}` in E && `${prefix}${reason}` in Z,
        ),
      ]).toEqual([reason, true]);
    for (const key of family("marvel0.ctl.seq.reason."))
      expect([
        key,
        [
          ...reasons,
          "stop_left_processes",
          "stop_all_client_connected",
          "stop_c_unknown",
        ],
      ]).toEqual([key, expect.arrayContaining([key])]);
    // The steps of a start through C, in the order the backend runs them.
    const chain = [
      ...act.match(/\nA_STEPS = \(([^)]*)\)/s)![1].matchAll(/"([a-z_]+)"/g),
      ...act.match(/\nCHAIN_STEPS = \(([^)]*)\)/s)![1].matchAll(/"([a-z_]+)"/g),
    ].map((m) => m[1]);
    expect(chain).toEqual([
      "monitor",
      "marker",
      "probe",
      "wait",
      "arm",
      "await_a",
      "b_probe",
      "b",
      "c_preflight",
      "c",
    ]);
    sameSet(family("marvel0.ctl.seq.chain."), chain);
    // What the wait before A can be waiting for, and its words in both languages.
    const holds = [
      ...act.match(/\nWAIT_HOLDS = \(([^)]*)\)/s)![1].matchAll(/"([a-z_]+)"/g),
    ].map((m) => m[1]);
    expect(holds.length).toBe(5);
    sameSet(family("marvel0.ctl.seq.hold."), holds);
    expect(missing("marvel0.ctl.seq.hold.", holds)).toEqual([]);
    // A stop ends with the single stop's own refusal code, worded by the
    // refusals' family, or by the sequence's own where the code has facts.
    for (const code of [
      "stop_failed",
      "not_ours",
      "unit_foreign",
      "stop_c_first",
    ])
      expect(`marvel0.ctl.block.${code}` in E).toBe(true);
    for (const code of ["stop_all_client_connected", "stop_left_processes"])
      expect(`marvel0.ctl.seq.reason.${code}` in E).toBe(true);
  });
});

describe("the hosted client's states, reasons, checks, notices and refusals", () => {
  test("the states and the nine actions", () => {
    sameSet(family("marvel0.run.state."), [...RUN_STATES]);
    sameSet(family("marvel0.run.act."), [...ACTIONS]);
    sameSet(
      [
        ...run.match(/\nSTATES = \(([^)]*)\)/s)![1].matchAll(/"([a-z_]+)"/g),
      ].map((m) => m[1]),
      [...RUN_STATES],
    );
    sameSet(
      [
        ...run.match(/\nACTIONS = \(([^)]*)\)/s)![1].matchAll(/"([a-z_]+)"/g),
      ].map((m) => m[1]),
      [...ACTIONS],
    );
  });

  test("every reason, why-not, check, notice and refusal of the backend has its sentence, and none is left over", () => {
    const pairs: Array<[string, string]> = [
      ["marvel0.run.reason.", "REASONS"],
      ["marvel0.run.why.", "WHY_NOT"],
      ["marvel0.run.check.", "CHECK_REFUSE"],
      ["marvel0.run.notice.", "NOTICES"],
    ];
    for (const [prefix, table] of pairs) {
      const codes = dictKeys(run, table);
      expect(codes.length).toBeGreaterThan(5);
      expect([prefix, missing(prefix, codes)]).toEqual([prefix, []]);
      sameSet(family(prefix), codes);
    }
    // The refusals that are not reasons for an action not being offered.
    const refusals = dictKeys(run, "REFUSALS");
    const why = new Set(dictKeys(run, "WHY_NOT"));
    const own = refusals.filter((code) => !why.has(code));
    expect(missing("marvel0.run.refuse.", own)).toEqual([]);
    for (const code of family("marvel0.run.refuse."))
      expect([...refusals, "format"]).toContain(code);
    // The checks that can pass are worded as refusals; the OK sentences are not needed.
    sameSet(
      dictKeys(run, "CHECK_OK"),
      dictKeys(run, "CHECK_REFUSE").concat(
        ["client_enabled"].filter(
          (c) => !dictKeys(run, "CHECK_REFUSE").includes(c),
        ),
      ),
    );
  });
});

describe("the wording rules of the Chinese text", () => {
  const ours = Object.keys(E).filter(
    (key) =>
      /^marvel0\.(ctl|run|chain)\./.test(key) ||
      key === "marvel0.cmd.follow" ||
      key === "marvel0.scope.gates",
  );

  test("no Chinese text is empty or the English sentence", () => {
    for (const key of ours) {
      expect(Z[key]).toBeTruthy();
      if (E[key].length > 12) expect(Z[key]).not.toBe(E[key]);
    }
  });

  test("a headline in Chinese has no Latin letter (names go in the text that follows)", () => {
    const headlines = ours.filter(
      (key) =>
        /(\.title|_title|\.summary)$/.test(key) ||
        key === "marvel0.ctl.seq.title",
    );
    expect(headlines.length).toBeGreaterThan(8);
    for (const key of headlines)
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

  test("the Chinese text keeps the placeholders of the English", () => {
    for (const key of ours) {
      const names = (text: string) =>
        [...text.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort();
      expect([key, names(Z[key])]).toEqual([key, names(E[key])]);
    }
  });
});

describe("what the page says about what the gates are worth", () => {
  test("no text of the controls calls anything an emergency stop: it only says that nothing is, or points at the physical one", () => {
    const allowed = [
      /not an emergency stop/gi,
      /nothing on this page is an emergency stop/gi,
      /nothing here is an emergency stop/gi,
      /is not an emergency stop/gi,
      /emergency stop or a safety check/gi,
      /(?:a hand is |hand )?on the physical e-stop/gi,
      /physical (?:emergency stop|e-stop)/gi,
      /the e-stop/gi,
    ];
    let checked = 0;
    for (const [key, text] of Object.entries(E)) {
      if (!/^marvel0\.(ctl|run|chain|scope|cmd)\./.test(key)) continue;
      if (!/emergency stop|e-stop/i.test(text)) continue;
      checked += 1;
      let rest = text;
      for (const rule of allowed) rest = rest.replace(rule, "");
      expect([key, rest.match(/emergency stop|e-stop/gi)]).toEqual([key, null]);
    }
    expect(checked).toBeGreaterThan(10);
  });

  test("the gates are not sold as a barrier against another program, and the page says what is not verified", () => {
    expect(E["marvel0.scope.gates"]).toContain("do not stop another program");
    expect(E["marvel0.stack.gates_scope"]).toContain("only prevent mistakes");
    expect(E["marvel0.run.notice.gates_scope"]).toContain(
      "stop mistakes, not another program",
    );
    expect(E["marvel0.ctl.honest"]).toContain("Not verified");
    expect(Z["marvel0.ctl.honest"]).toContain("还没有验证");
    for (const key of [
      "marvel0.scope.gates",
      "marvel0.run.notice.gates_scope",
      "marvel0.ctl.honest",
    ])
      expect(Z[key]).toMatch(/防不了|不能|没有/);
  });
});
