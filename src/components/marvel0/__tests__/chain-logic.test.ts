import { describe, expect, test } from "bun:test";
import en from "@/i18n/en.json";
import { chainView, CHAIN, type ChainInput } from "../chain-logic";
import { readDeploy } from "../deploy-logic";
import { stackFromCards, type Marvel0Card } from "../marvel0-logic";
import { runView } from "../run-logic";
import { actView } from "../stack-act-logic";
import { stackView } from "../stack-status-logic";
import { idle, ready, starting } from "./deploy-fixtures";
import { runDoc, stackDoc } from "./ctl-fixtures";

const KNOWN = new Set(Object.keys(en));

const stack = (name: string, change?: (doc: Record<string, any>) => void) => {
  // eslint-disable-line @typescript-eslint/no-explicit-any
  const doc = stackDoc(name, change);
  const passive = stackView(doc, KNOWN)!;
  return { passive, act: actView(doc, KNOWN, passive) };
};
const run = (name: string) => runView(runDoc(name), KNOWN);
/** The samples carry the gripper test that failed from `gripper_failed` on;
 * the ones that stand for a healthy stack have a later test that passed. */
const testPassed = (doc: Record<string, unknown>) => {
  const act = doc.act as { gripper_test: { last: unknown } };
  act.gripper_test.last = {
    action: "close",
    ok: true,
    code: "reached",
    reached: true,
    stalled: false,
    at: doc.sampled_at,
  };
};

const input = (over: Partial<ChainInput> = {}): ChainInput => ({
  strip: null,
  stack: null,
  deploy: null,
  run: null,
  ...over,
});
const states = (chain: ReturnType<typeof chainView>) =>
  Object.fromEntries(chain.nodes.map((n) => [n.id, n.state]));

const card = (
  id: Marvel0Card["id"],
  state: Marvel0Card["state"],
): Marvel0Card => ({
  id,
  title: "t",
  state,
  summary: { key: "k" },
  facts: [],
  notes: [],
  hint: "h",
});

describe("the chain", () => {
  test("seven nodes in the order things come up", () => {
    expect(CHAIN).toEqual(["M", "desk", "A", "B", "C", "policy", "run"]);
    expect(chainView(input()).nodes.map((n) => n.id)).toEqual([...CHAIN]);
  });

  test("nothing read: every node is unknown, never idle or ready", () => {
    const chain = chainView(input());
    for (const node of chain.nodes) expect(node.state).toBe("unknown");
    expect(chain.focus?.id).toBe("M");
  });

  test("the first node that is not ready is the one to look at", () => {
    const cards = [
      "monitor",
      "desk",
      "arm",
      "gripper",
      "robot_server",
      "policy",
      "demo",
    ] as const;
    const strip = stackFromCards(
      cards.map((id) => card(id, id === "arm" ? "not_started" : "ok")),
      null,
    );
    const chain = chainView(input({ strip }));
    expect(chain.focus?.id).toBe("A");
    expect(states(chain)).toMatchObject({
      M: "ready",
      desk: "ready",
      A: "idle",
      B: "ready",
    });
  });

  test("a failed terminal is named as the problem, with its failure, ahead of the idle nodes after it", () => {
    const chain = chainView(
      input({
        strip: stackFromCards([card("desk", "ok")], null),
        stack: stack("a_failed"),
        deploy: readDeploy(idle()),
      }),
    );
    expect(chain.focus).toMatchObject({ id: "A", kind: "problem" });
    expect(states(chain).A).toBe("problem");
    expect(chain.focus?.reason).not.toBeNull();
  });

  test("a node that cannot be read says so instead of saying nothing", () => {
    const chain = chainView(input());
    expect(chain.focus).toMatchObject({ id: "M", kind: "blocked" });
    expect(chain.focus?.reason?.say).toEqual({
      key: "marvel0.chain.reason.unknown",
    });
  });

  test("everything ready: no node is marked and the chain says so", () => {
    const chain = chainView(
      input({
        strip: stackFromCards(
          [card("desk", "ok"), card("policy", "ok"), card("demo", "ok")],
          null,
        ),
        stack: stack("c_ready", testPassed),
        deploy: readDeploy(ready()),
        run: run("running"),
      }),
    );
    expect(chain.focus).toBeNull();
    for (const node of chain.nodes) expect(node.state).toBe("ready");
  });

  test("a gripper test that failed keeps B from being ready: 'Ready, test failed', the one to look at, until a test passes", () => {
    const failed = chainView(
      input({
        strip: stackFromCards([card("desk", "ok")], null),
        stack: stack("gripper_failed"),
      }),
    );
    const b = failed.nodes.find((n) => n.id === "B")!;
    expect([b.state, b.detail, b.startable]).toEqual([
      "attention",
      "test_failed",
      false,
    ]);
    expect(b.reason?.say).toEqual({ key: "marvel0.chain.reason.test_failed" });
    expect(failed.focus).toMatchObject({ id: "B", kind: "check" });
    // A later test that passes clears it: B is ready and the focus moves on.
    const passed = chainView(
      input({
        strip: stackFromCards([card("desk", "ok")], null),
        stack: stack("gripper_failed", testPassed),
      }),
    );
    const clear = passed.nodes.find((n) => n.id === "B")!;
    expect([clear.state, clear.detail]).toEqual(["ready", undefined]);
    expect(passed.focus?.id).not.toBe("B");
    // A gripper stack that is not ready is told by its state, not by its test.
    const down = chainView(
      input({
        stack: stack("gripper_failed", (doc) => {
          doc.act.terminals.b.state = "idle";
        }),
      }),
    );
    expect(down.nodes.find((n) => n.id === "B")?.detail).toBeUndefined();
  });
});

describe("what each node is made from", () => {
  test("M, A, B and C follow the stack's controls when they are there", () => {
    const chain = chainView(input({ stack: stack("a_starting") }));
    expect(states(chain)).toMatchObject({
      M: "ready",
      A: "working",
      B: "idle",
      C: "idle",
    });
    expect(chain.focus).toMatchObject({ id: "desk" });
  });

  test("the node that is starting says how long it has waited", () => {
    const strip = stackFromCards([card("desk", "ok")], null);
    const chain = chainView(input({ strip, stack: stack("a_starting") }));
    expect(chain.focus).toMatchObject({ id: "A", kind: "working" });
    expect(chain.focus?.reason?.say).toEqual({
      key: "marvel0.chain.reason.starting_waited",
      params: { s: 12 },
    });
  });

  test("a node that cannot be started says why in the backend's words; one that can says so", () => {
    const strip = stackFromCards([card("desk", "ok")], null);
    const blocked = chainView(input({ strip, stack: stack("idle") }));
    expect(blocked.focus).toMatchObject({ id: "M", kind: "next" });
    const next = chainView(input({ strip, stack: stack("ready_to_start_a") }));
    expect(next.focus).toMatchObject({ id: "A", kind: "next" });
    const desk = chainView(
      input({
        strip: stackFromCards([card("desk", "unknown")], null),
        stack: stack("monitor_ready"),
      }),
    );
    expect(desk.focus).toMatchObject({ id: "desk" });
    const b = chainView(
      input({
        strip: stackFromCards([card("desk", "ok")], null),
        stack: stack("a_ready", (doc) => {
          doc.act.terminals.b.can_start = false;
        }),
      }),
    );
    expect(b.focus).toMatchObject({ id: "B", kind: "blocked" });
    expect(b.focus?.reason).not.toBeNull();
  });

  test("without the controls the passive stack card decides; without that, the status cards", () => {
    const passiveOnly = stackDoc("idle");
    delete passiveOnly.act;
    const passive = stackView(passiveOnly, KNOWN)!;
    const chain = chainView(input({ stack: { passive, act: null } }));
    expect(states(chain)).toMatchObject({ M: "idle", A: "idle", C: "idle" });
    const cards = chainView(
      input({
        strip: stackFromCards(
          [
            card("monitor", "stale"),
            card("arm", "red"),
            card("robot_server", "not_started"),
          ],
          null,
        ),
      }),
    );
    expect(states(cards)).toMatchObject({
      M: "stale",
      A: "problem",
      C: "idle",
    });
  });

  test("the policy server follows the deploy state, the run follows the hosted client", () => {
    expect(
      states(chainView(input({ deploy: readDeploy(starting()) }))).policy,
    ).toBe("working");
    expect(
      states(chainView(input({ deploy: readDeploy(ready()) }))).policy,
    ).toBe("ready");
    expect(states(chainView(input({ run: run("running") }))).run).toBe("ready");
    expect(states(chainView(input({ run: run("held") }))).run).toBe(
      "attention",
    );
    expect(states(chainView(input({ run: run("lost") }))).run).toBe("problem");
    expect(states(chainView(input({ run: run("idle_ready") }))).run).toBe(
      "idle",
    );
  });

  test("a run that cannot be started says why, in the backend's words", () => {
    const chain = chainView(
      input({
        strip: stackFromCards(
          ["monitor", "desk", "arm", "gripper", "robot_server", "policy"].map(
            (id) => card(id as Marvel0Card["id"], "ok"),
          ),
          null,
        ),
        run: run("preflight_failing"),
      }),
    );
    expect(chain.focus).toMatchObject({ id: "run" });
    expect(chain.focus?.reason).not.toBeNull();
  });
});
