// The chain at the top of the page: M, Desk, A, B, C, the policy server and
// the evaluation run, each with a state and, for the first one that is not
// ready, the reason. Pure: every node's state comes from the best source the
// page has for it (the arm stack's controls, else the passive stack card,
// else the status card), never from a guess; a source that cannot be read
// makes the node "unknown", not "idle".
//
// The chain is the order things must come up in. The first node that is not
// ready is the one the person looks at: a problem, something still starting,
// something that cannot be started yet (with the backend's reason), or the
// next step that can be taken now.
import { stopping } from "./ctl-text";
import type { DeployState, DeployStatus } from "./deploy-logic";
import type { Worded } from "./marvel0-json";
import type {
  Marvel0CardId,
  Marvel0State,
  Say,
  StackStripData,
} from "./marvel0-logic";
import {
  gripperTestFailed,
  startControl,
  type ActView,
  type CompId,
  type CompState,
} from "./stack-act-logic";
import type { MemberState, StackView } from "./stack-status-logic";
import type { RunState, RunView } from "./run-logic";

export const CHAIN = ["M", "desk", "A", "B", "C", "policy", "run"] as const;
export type ChainId = (typeof CHAIN)[number];

export type NodeState =
  | "ready"
  | "working"
  | "idle"
  | "problem"
  | "attention"
  | "stale"
  | "unknown";

export const NODE_TONE: Record<
  NodeState,
  "success" | "info" | "neutral" | "danger" | "warning"
> = {
  ready: "success",
  working: "info",
  idle: "neutral",
  problem: "danger",
  attention: "warning",
  stale: "warning",
  unknown: "warning",
};
/** The catalogue key of each state's word. */
export const nodeStateKey = (state: NodeState): string =>
  `marvel0.chain.state.${state}`;

export const NODE_LABEL: Record<ChainId, string> = {
  M: "marvel0.strip.monitor",
  desk: "marvel0.strip.desk",
  A: "marvel0.strip.arm",
  B: "marvel0.strip.gripper",
  C: "marvel0.strip.server",
  policy: "marvel0.strip.policy",
  run: "marvel0.chain.node.run",
};

/** The id of the element a node's click scrolls to. */
export const nodeAnchor = (id: ChainId): string => `marvel0-node-${id}`;
/** The id of the command card (step 3), where a run is started: the idle Run
 * card points there. */
export const COMMAND_ANCHOR = "marvel0-command-card";

export interface ChainNode {
  id: ChainId;
  state: NodeState;
  /** Why, in the page's words or the server's; null when nothing is added to
   * the state. */
  reason: Worded<Say> | null;
  /** This node can be started now (only the nodes with a start button). */
  startable: boolean;
  /** The node is ready, and something about it still wants a look: B after a
   * gripper test that failed. It reads "Ready · test failed" (warning). */
  detail?: "test_failed";
}

export type FocusKind = "problem" | "working" | "blocked" | "next" | "check";
export interface Focus {
  id: ChainId;
  kind: FocusKind;
  reason: Worded<Say> | null;
}

export interface Chain {
  nodes: ChainNode[];
  /** The first node that is not ready; null when every node is. */
  focus: Focus | null;
}

const say = (
  key: string,
  params?: Record<string, string | number | Say>,
): Say => (params ? { key, params } : { key });
const plain = (s: Say): Worded<Say> => ({ say: s, text: "" });

// -- the sources

const FROM_COMP: Record<CompState, NodeState> = {
  idle: "idle",
  starting: "working",
  stopping: "working",
  ready: "ready",
  failed: "problem",
  dead: "problem",
  unknown: "unknown",
  foreign_running: "attention",
};
const FROM_MEMBER: Record<MemberState, NodeState> = {
  running: "ready",
  starting: "working",
  stopping: "working",
  stopped: "idle",
  failed: "problem",
  foreign: "attention",
  elsewhere: "attention",
  unknown: "unknown",
};
const FROM_CARD: Record<Marvel0State, NodeState> = {
  ok: "ready",
  red: "problem",
  stale: "stale",
  not_started: "idle",
  unknown: "unknown",
};
const FROM_DEPLOY: Record<DeployState, NodeState> = {
  idle: "idle",
  starting: "working",
  ready: "ready",
  failed: "problem",
  dead: "problem",
  unknown: "unknown",
  foreign_listener: "attention",
  stopping: "working",
};
const FROM_RUN: Record<RunState, NodeState> = {
  idle: "idle",
  preflight: "working",
  starting: "working",
  at_gate: "ready",
  homing: "ready",
  running: "ready",
  awaiting_label: "ready",
  finishing: "ready",
  done: "idle",
  failed: "problem",
  held: "attention",
  lost: "problem",
};

const CARD_OF: Record<ChainId, Marvel0CardId> = {
  M: "monitor",
  desk: "desk",
  A: "arm",
  B: "gripper",
  C: "robot_server",
  policy: "policy",
  run: "demo",
};

export interface ChainInput {
  /** The status cards' strip (always there once the page has loaded). */
  strip: StackStripData | null;
  /** The arm stack document, when its last read is current. */
  stack: { passive: StackView; act: ActView | null } | null;
  /** The policy server's state, when its last read is current. */
  deploy: DeployStatus | null;
  /** The hosted client's state, when its last read is current. */
  run: RunView | null;
}

function fromCard(input: ChainInput, id: ChainId): ChainNode {
  const step = input.strip?.steps.find((s) => s.id === CARD_OF[id]);
  const state = step ? FROM_CARD[step.state] : "unknown";
  // Why a node that is not ready cannot be judged, or is not the page's to start.
  const reason: Worded<Say> | null =
    state === "unknown"
      ? plain(say("marvel0.chain.reason.unknown"))
      : state === "stale"
        ? plain(say("marvel0.chain.reason.stale"))
        : id === "desk" && state !== "ready"
          ? plain(say("marvel0.chain.reason.desk"))
          : null;
  return { id, state, reason, startable: false };
}

function compNode(input: ChainInput, id: CompId): ChainNode {
  const act = input.stack?.act ?? null;
  if (act) {
    const comp = act.comps[id];
    const start = startControl(comp);
    const state = FROM_COMP[comp.state];
    let reason: Worded<Say> | null = null;
    // B is up but its last gripper test failed: not "ready" until a test
    // passes (a later test replaces the backend's `last`).
    if (id === "B" && comp.state === "ready" && gripperTestFailed(act.gripper))
      return {
        id,
        state: "attention",
        reason: plain(say("marvel0.chain.reason.test_failed")),
        startable: false,
        detail: "test_failed",
      };
    if (comp.state === "starting") {
      const waited = comp.readiness?.waitedS;
      reason = plain(
        waited === null || waited === undefined
          ? say("marvel0.chain.reason.starting")
          : say("marvel0.chain.reason.starting_waited", {
              s: Math.round(waited),
            }),
      );
    } else if (comp.state === "stopping")
      reason = plain(say("marvel0.chain.reason.stopping"));
    else if (state === "problem") reason = comp.failure ?? comp.reason;
    else if (state === "ready") reason = null;
    else if (!start.enabled) {
      const first = stopping(start.blockers)[0];
      reason = first ? first.worded : comp.reason;
    } else reason = plain(say("marvel0.chain.reason.can_start"));
    return { id, state, reason, startable: start.enabled };
  }
  const member = input.stack?.passive.members.find((m) => m.id === id);
  if (member)
    return {
      id,
      state: FROM_MEMBER[member.state],
      reason: { say: member.why, text: "" },
      startable: false,
    };
  return fromCard(input, id);
}

function policyNode(input: ChainInput): ChainNode {
  const status = input.deploy;
  if (!status) return fromCard(input, "policy");
  return {
    id: "policy",
    state: FROM_DEPLOY[status.state],
    reason: null,
    startable:
      status.canDeploy && ["idle", "failed", "dead"].includes(status.state),
  };
}

function runNode(input: ChainInput): ChainNode {
  const run = input.run;
  if (!run) return fromCard(input, "run");
  const first = run.preflight.find((row) => row.level !== "ok");
  const enter = run.actions.start;
  return {
    id: "run",
    state: FROM_RUN[run.state],
    reason:
      run.state === "idle" || run.state === "done"
        ? (enter.whyNot ?? first?.worded ?? null)
        : run.reason,
    startable: enter.allowed,
  };
}

function node(input: ChainInput, id: ChainId): ChainNode {
  switch (id) {
    case "desk":
      return fromCard(input, "desk");
    case "policy":
      return policyNode(input);
    case "run":
      return runNode(input);
    default:
      return compNode(input, id);
  }
}

/** The chain from the best source of each node, and the node to look at. */
export function chainView(input: ChainInput): Chain {
  const nodes = CHAIN.map((id) => node(input, id));
  const first = nodes.find((n) => n.state !== "ready");
  let focus: Focus | null = null;
  if (first) {
    const kind: FocusKind =
      first.state === "problem"
        ? "problem"
        : first.state === "working"
          ? "working"
          : first.detail === "test_failed"
            ? "check"
            : first.startable
              ? "next"
              : "blocked";
    focus = { id: first.id, kind, reason: first.reason };
  }
  return { nodes, focus };
}
