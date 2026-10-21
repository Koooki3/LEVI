import { describe, expect, test } from "bun:test";
import en from "@/i18n/en.json";
import { STATE_IDS } from "../autonomy-doc";
import type { AeriFact } from "../autonomy-logic";
import { chainView, type ChainInput, type NodeState } from "../chain-logic";
import { readDeploy } from "../deploy-logic";
import { stackFromCards, type Marvel0Card } from "../marvel0-logic";
import { runView } from "../run-logic";
import { actView, canStopSequence } from "../stack-act-logic";
import { stackView } from "../stack-status-logic";
import { ready as deployReady } from "./deploy-fixtures";
import { runDoc as eRunDoc, stackDoc } from "./ctl-fixtures";
import { blockersOf } from "../ctl-text";

// The chain's run node follows the evaluation client AND the terminal D of
// step 5 (review R11: it followed only the client). A D that lives IS a run: it
// holds the arm lock and the policy, and the backend refuses the stack's stop
// and start, and the client's start, while it does (`run_active`).

const KNOWN = new Set(Object.keys(en));
const E = en as Record<string, string>;

const card = (id: Marvel0Card["id"]): Marvel0Card => ({
  id,
  title: "t",
  state: "ok",
  summary: { key: "k" },
  facts: [],
  notes: [],
  hint: "h",
});
const every = stackFromCards(
  (
    [
      "monitor",
      "desk",
      "arm",
      "gripper",
      "robot_server",
      "policy",
      "demo",
    ] as const
  ).map(card),
  null,
);
const input = (over: Partial<ChainInput> = {}): ChainInput => ({
  strip: every,
  stack: null,
  deploy: readDeploy(deployReady()),
  run: null,
  ...over,
});
const fact = (
  state: AeriFact["state"],
  over: Partial<AeriFact> = {},
): AeriFact => ({
  alive: true,
  state,
  current: true,
  ...over,
});
const runNode = (chain: ReturnType<typeof chainView>) =>
  chain.nodes.find((n) => n.id === "run")!;

describe("the chain's run node knows terminal D", () => {
  test("a D that lives is a run: it is not 'not started', whatever the client's document says", () => {
    const withoutD = chainView(
      input({ run: runView(eRunDoc("idle_ready"), KNOWN) }),
    );
    expect(runNode(withoutD).state).toBe("idle");
    for (const state of [
      "await_release",
      "forward_running",
      "reverse_running",
    ] as const) {
      const node = runNode(
        chainView(
          input({
            run: runView(eRunDoc("idle_ready"), KNOWN),
            aeri: fact(state),
          }),
        ),
      );
      expect([state, node.state]).toEqual([state, "ready"]);
      expect(node.startable).toBe(false);
    }
  });

  test("every state of D has a node state: starting and stopping are 'working', stuck, locked and unreadable are problems, a restart's orphan needs a look, none is 'not started' while D lives", () => {
    const expected: Record<string, NodeState> = {
      idle: "idle",
      ended: "idle",
      launching: "working",
      services: "working",
      stopping: "working",
      orphaned: "attention",
      stalled: "problem",
      fault_locked: "problem",
      unknown: "problem",
    };
    for (const state of STATE_IDS) {
      const node = runNode(chainView(input({ aeri: fact(state) })));
      expect([state, node.state]).toEqual([state, expected[state] ?? "ready"]);
    }
  });

  test("a D that is not alive leaves the node to the client: nothing changes", () => {
    const e = runView(eRunDoc("running"), KNOWN);
    const plain = chainView(input({ run: e }));
    for (const state of STATE_IDS) {
      const chain = chainView(
        input({ run: e, aeri: fact(state, { alive: false }) }),
      );
      expect(runNode(chain)).toEqual(runNode(plain));
    }
    expect(runNode(chainView(input({ run: e, aeri: null }))).state).toBe(
      runNode(plain).state,
    );
  });

  test("a D read too long ago is stale, not ready: the chain does not call a run it cannot see healthy", () => {
    const node = runNode(
      chainView(input({ aeri: fact("forward_running", { current: false }) })),
    );
    expect(node.state).toBe("stale");
    expect(node.reason?.say).toEqual({ key: "marvel0.chain.reason.stale" });
  });

  test("D wins over the client's document while it lives (the two cannot be live together; if both say so, the arm is in use)", () => {
    const node = runNode(
      chainView(
        input({
          run: runView(eRunDoc("idle_ready"), KNOWN),
          aeri: fact("launching"),
        }),
      ),
    );
    expect(node.state).toBe("working");
  });

  test("the sentence under the chain says what D holds and what stays off; a D in trouble is worded by its own state", () => {
    const working = chainView(input({ aeri: fact("launching") }));
    expect(working.focus).toMatchObject({ id: "run", kind: "working" });
    expect(working.focus?.reason?.say).toEqual({
      key: "marvel0.autonomy.chain.live",
    });
    expect(E["marvel0.autonomy.chain.live"]).toMatch(/holds the arm/);
    expect(E["marvel0.autonomy.chain.live"]).toMatch(/stop and start windows/);
    const stuck = chainView(input({ aeri: fact("stalled") }));
    expect(stuck.focus).toMatchObject({ id: "run", kind: "problem" });
    expect(stuck.focus?.reason?.say).toEqual({
      key: "marvel0.autonomy.hint.stalled",
    });
    const lost = chainView(input({ aeri: fact("orphaned") }));
    expect(lost.focus?.reason?.say).toEqual({
      key: "marvel0.autonomy.hint.orphaned",
    });
  });

  test("with every other link ready and D running well, the chain is ready", () => {
    const chain = chainView(input({ aeri: fact("forward_running") }));
    // (the arm stack nodes come from the strip's cards: all ok)
    expect(runNode(chain).state).toBe("ready");
  });
});

describe("the stack's stop and start windows while D lives", () => {
  // The backend refuses them with the row `run_active` (it counts the hosted
  // terminal D through run_api.run_active). The page shows the backend's row;
  // this is the shape the backend writes for it (stack_act.row).
  const row = {
    code: "run_active",
    level: "required",
    text: "A hosted evaluation run is active (or cannot be told); end it first.",
    params: {},
  };

  test("a stop of the whole stack the backend refuses for `run_active` is not offered, and the blocker is worded in the page's words", () => {
    const doc = stackDoc("stop_all_ready");
    const passive = stackView(doc, KNOWN)!;
    expect(canStopSequence(actView(doc, KNOWN, passive)!)).toBe(true);
    doc.act.stop_all_blockers = [row];
    doc.act.checks.can_stop_all = false;
    const act = actView(doc, KNOWN, passive)!;
    expect(canStopSequence(act)).toBe(false);
    expect(act.stopAll.blockers.map((b) => b.code)).toEqual(["run_active"]);
    expect(blockersOf([row], KNOWN)[0].worded.say).toEqual({
      key: "marvel0.ctl.block.run_active",
    });
  });
});
