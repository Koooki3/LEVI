import { describe, expect, test } from "bun:test";
import { readFileSync } from "fs";
import { join } from "path";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import {
  GATED,
  MEMBERS,
  STACK_SCHEMA,
  newRequestId,
  phraseMatches,
  stackView,
} from "../stack-status-logic";

const KNOWN = new Set(Object.keys(en));
const root = join(import.meta.dir, "../../../..");
const backend = readFileSync(join(root, "levi/marvel0/stack.py"), "utf8");

/** The codes the backend can answer with, read from its source, so a code it
 * gains that the card cannot word fails here. */
function codes(pattern: RegExp): string[] {
  return [...new Set([...backend.matchAll(pattern)].map((m) => m[1]))];
}
const REFUSALS = (() => {
  const start = backend.indexOf("\nREFUSALS = {");
  const end = backend.indexOf("\n}\n", start);
  return [...backend.slice(start, end).matchAll(/^ {4}"([a-z_]+)": /gm)].map(
    (m) => m[1],
  );
})();
const TERMINAL = [
  ...codes(/"((?:m|a|b|c|policy|client)\.[a-z_]+)"/g),
  // Built from parts in terminals() (f"m.{state}", f"m.{unit}_{file}").
  ...["running", "starting", "stopping", "failed", "foreign"].map(
    (s) => `m.${s}`,
  ),
  "m.absent_health_stale",
  "m.absent_no_health",
  "m.stopped_health_stale",
  "m.stopped_no_health",
].filter(
  (code) =>
    !code.startsWith("m.pid") &&
    !/\.(arm_sockets|process_seen|hardware_active|listening|impedance_active|connected|open_session)$/.test(
      code,
    ),
);
const EVIDENCE = [
  ...codes(/_evidence\(\s*"([a-z_.]+)"/g),
  ...codes(/_evidence\(\s*\n\s*"([a-z_.]+)"/g),
];
const GATES = codes(/"(gate\.[a-z_]+)"/g);
const PLANS = [
  ...codes(/"(auto\.[a-z_]+)"/g),
  "auto.leave_foreign",
  "auto.leave_elsewhere",
  "auto.leave_unknown",
];

describe("the catalogue words every code of the stack backend", () => {
  const missing = (keys: string[]) =>
    keys.filter((k) => !(k in en) || !(k in zh));
  test("refusals", () => {
    expect(REFUSALS.length).toBeGreaterThan(15);
    expect(missing(REFUSALS.map((c) => `marvel0.stack.refuse.${c}`))).toEqual(
      [],
    );
  });
  test("member states and their reasons", () => {
    expect(missing(TERMINAL.map((c) => `marvel0.stack.code.${c}`))).toEqual([]);
  });
  test("evidence", () => {
    expect(EVIDENCE).toContain("health.age_s");
    expect(missing(EVIDENCE.map((c) => `marvel0.stack.evidence.${c}`))).toEqual(
      [],
    );
  });
  test("gate checks", () => {
    expect(GATES).toContain("gate.fci_probe");
    expect(missing(GATES.map((c) => `marvel0.stack.check.${c}`))).toEqual([]);
  });
  test("automatic steps", () => {
    expect(missing(PLANS.map((c) => `marvel0.stack.plan.${c}`))).toEqual([]);
  });
});

const term = (state: string, code: string, evidence: unknown[] = []) => ({
  state,
  code,
  evidence,
});
const check = (code: string, ok: boolean | null, level = "required") => ({
  code,
  ok,
  level,
  params: {},
});

/** Today's state (2026-10-20 13:5x): A and C started by hand, the monitor
 * unit gone and its health file stale, the marker set. */
function sample(over: Record<string, unknown> = {}) {
  return {
    schema: STACK_SCHEMA,
    sampled_at: 1_800_000_000,
    profile: "real",
    profile_code: "profile.product",
    lock: { state: "free", holder: null, file: ".marvel0-stack.lock" },
    linger: { state: "no" },
    rt_limits: { state: "ok", rtprio: 99, memlock: "infinity" },
    arm_sockets: { state: "active", detail: "" },
    health: { state: "offline", age_s: 1200 },
    marker: {
      state: "present",
      age_s: 5000,
      ctime_age_s: 5000,
      can_clear: false,
      clear_blockers: ["marker_arm_active"],
    },
    terminals: {
      M: term("stopped", "m.absent_health_stale", [
        { code: "health.state", value: "offline" },
        { code: "health.age_s", value: 1200 },
      ]),
      A: term("running", "a.running", [
        { code: "a.arm_sockets", value: "active" },
      ]),
      B: term("unknown", "b.not_observable"),
      C: term("running", "c.running"),
      policy: term("stopped", "policy.stopped"),
      client: term("stopped", "client.stopped"),
    },
    gates: {
      A: {
        verdict: "would_refuse",
        reasons: ["gate.a_absent"],
        route: false,
        checks: [
          check("gate.product", true),
          check("gate.a_absent", false),
          check("gate.fci_probe", null),
          check("gate.linger", false, "warn"),
          check("gate.person_desk", null, "person"),
        ],
      },
      B: { verdict: "would_refuse", reasons: [], route: false, checks: [] },
      C: { verdict: "would_refuse", reasons: [], route: false, checks: [] },
    },
    monitor: {
      unit: "levi-fr3-health-monitor.service",
      unit_state: "absent",
      owned: false,
      can_start: true,
      start_blockers: [],
      can_stop: false,
      stop_blockers: ["monitor_not_ours", "stack_running"],
      auto: true,
      plan: { action: "start", code: "auto.start_for_stack" },
      last_auto: null,
    },
    phrases: {
      monitor_start: "start health monitor",
      monitor_stop: "stop health monitor",
      marker_clear: "clear marker",
    },
    notices: [
      { code: "notice.gates_scope" },
      { code: "notice.logout_stops_stack" },
    ],
    verdict: { verdict: "busy", active: ["A", "C"], unknown: ["B"] },
    ...over,
  };
}

describe("stackView", () => {
  test("anything but a stack document is null", () => {
    expect(stackView(null, KNOWN)).toBeNull();
    expect(stackView({ schema: "other" }, KNOWN)).toBeNull();
    expect(stackView([], KNOWN)).toBeNull();
  });

  test("the rows come in a fixed order, and a missing monitor says why", () => {
    const view = stackView(sample(), KNOWN)!;
    expect(view.members.map((m) => m.id)).toEqual([...MEMBERS]);
    const m = view.members[0];
    expect(m.state).toBe("stopped");
    expect(m.why).toEqual({
      key: "marvel0.stack.code.m.absent_health_stale",
      params: { age: { ago: 1200 } },
    });
    expect(m.evidence[1]).toEqual({
      label: { key: "marvel0.stack.evidence.health.age_s" },
      value: { raw: "1200" },
    });
    expect(view.members[2].why).toEqual({
      key: "marvel0.stack.code.b.not_observable",
      params: { age: { ago: 1200 } },
    });
  });

  test("the gates are a dry run: warnings and the person's checks never pass or fail", () => {
    const view = stackView(sample(), KNOWN)!;
    expect(view.gates.map((g) => g.id)).toEqual([...GATED]);
    const outcomes = view.gates[0].checks.map((c) => c.outcome);
    expect(outcomes).toEqual(["pass", "fail", "unknown", "warn", "person"]);
    expect(view.gates[0].verdict).toBe("would_refuse");
  });

  test("an unknown verdict is a refusal and an unknown state is unknown", () => {
    const raw = sample();
    (raw.gates as Record<string, unknown>).A = { verdict: "maybe", checks: [] };
    (raw.terminals as Record<string, unknown>).A = term("dancing", "a.x");
    const view = stackView(raw, KNOWN)!;
    expect(view.gates[0].verdict).toBe("would_refuse");
    expect(view.members[1].state).toBe("unknown");
    expect(view.members[1].why).toEqual({ raw: "a.x" });
  });

  test("outside the product nothing can be pressed, whatever the server says", () => {
    const view = stackView(
      sample({
        profile: "fake",
        marker: { state: "present", can_clear: true, clear_blockers: [] },
      }),
      KNOWN,
    )!;
    expect(view.real).toBe(false);
    expect(view.monitor.canStart).toBe(false);
    expect(view.marker.canClear).toBe(false);
    expect(view.notices[0]).toEqual({ key: "marvel0.stack.fake" });
  });

  test("the gates' scope is always said, and the logout notice when sent", () => {
    const keys = (raw: unknown) =>
      stackView(raw, KNOWN)!.notices.map((n) => ("key" in n ? n.key : ""));
    expect(keys(sample())).toEqual([
      "marvel0.stack.gates_scope",
      "marvel0.stack.logout",
    ]);
    expect(keys(sample({ notices: [] }))).toEqual([
      "marvel0.stack.gates_scope",
    ]);
  });

  test("blockers and the automatic plan are worded", () => {
    const view = stackView(sample(), KNOWN)!;
    expect(view.monitor.stopBlockers).toEqual([
      { key: "marvel0.stack.refuse.monitor_not_ours" },
      { key: "marvel0.stack.refuse.stack_running" },
    ]);
    expect(view.monitor.plan).toEqual({
      key: "marvel0.stack.plan.auto.start_for_stack",
    });
    expect(view.marker.label).toEqual({
      key: "marvel0.stack.marker.present",
      params: { age: { ago: 5000 } },
    });
  });
});

describe("rule R", () => {
  test("only an explicit busy or idle is taken; anything else is unknown", () => {
    expect(stackView(sample(), KNOWN)!.verdict).toBe("busy");
    const idle = sample({
      verdict: { verdict: "idle", active: [], unknown: [] },
    });
    expect(stackView(idle, KNOWN)!.verdict).toBe("idle");
    expect(stackView(sample({ verdict: undefined }), KNOWN)!.verdict).toBe(
      "unknown",
    );
    expect(
      stackView(sample({ verdict: { verdict: "quiet" } }), KNOWN)!.verdict,
    ).toBe("unknown");
  });

  test("a check's parameters fill its sentence", () => {
    const raw = sample();
    (raw.gates as Record<string, { checks: unknown[] }>).C.checks = [
      {
        code: "gate.layers_known",
        ok: false,
        level: "required",
        params: { unknown: "B, client" },
      },
    ];
    const [row] = stackView(raw, KNOWN)!.gates[2].checks;
    expect(row.label).toEqual({
      key: "marvel0.stack.check.gate.layers_known",
      params: { unknown: "B, client" },
    });
    expect(row.outcome).toBe("fail");
  });
});

describe("requests", () => {
  test("a request id is one the server accepts", () => {
    for (let i = 0; i < 20; i++)
      expect(newRequestId()).toMatch(/^[A-Za-z0-9._:-]{8,80}$/);
  });
  test("the phrase must match exactly", () => {
    expect(phraseMatches(" clear marker ", "clear marker")).toBe(true);
    expect(phraseMatches("clear", "clear marker")).toBe(false);
    expect(phraseMatches("Clear marker", "clear marker")).toBe(false);
  });
});
