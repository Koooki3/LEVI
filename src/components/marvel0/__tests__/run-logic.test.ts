import { describe, expect, test } from "bun:test";
import { readFileSync } from "fs";
import { join } from "path";
import en from "@/i18n/en.json";
import {
  ACTIONS,
  RUN_STATES,
  armedActions,
  holdEnabled,
  mayMove,
  purposeOf,
  runPhrase,
  runView,
  startPhrase,
  stepBar,
} from "../run-logic";
import { RUN_VIEWS, runDoc } from "./ctl-fixtures";

const KNOWN = new Set(Object.keys(en));
const view = (
  name: string,
  change?: (doc: Record<string, any>) => void, // eslint-disable-line @typescript-eslint/no-explicit-any
) => runView(runDoc(name, change), KNOWN)!;
const allowed = (name: string) =>
  ACTIONS.filter((id) => view(name).actions[id].allowed);

describe("the hosted client's document", () => {
  test("every named view of the backend reads, with the backend's own state", () => {
    expect(RUN_VIEWS.length).toBeGreaterThanOrEqual(25);
    for (const name of RUN_VIEWS) {
      const doc = runDoc(name);
      const read = view(name);
      expect(read.state).toBe(doc.state);
      expect(RUN_STATES).toContain(read.state);
      for (const id of ACTIONS)
        expect(read.actions[id].allowed).toBe(doc.actions[id].allowed);
    }
  });

  test("anything but a run document is no document", () => {
    expect(runView(null, KNOWN)).toBeNull();
    expect(runView({ schema: "levi.marvel0.run.v9" }, KNOWN)).toBeNull();
    expect(runView([], KNOWN)).toBeNull();
  });

  test("an unknown state reads as no client, and nothing is offered that the backend did not offer", () => {
    const odd = view("at_gate", (doc) => {
      doc.state = "dancing";
      for (const entry of Object.values<any>(doc.actions))
        entry.allowed = "yes"; // eslint-disable-line @typescript-eslint/no-explicit-any
    });
    expect(odd.state).toBe("idle");
    for (const id of ACTIONS) expect(odd.actions[id].allowed).toBe(false);
  });

  test("the episode, the steps and the reason with its facts", () => {
    const running = view("running");
    expect(running.episode).toEqual({
      no: 1,
      step: 137,
      max: 400,
      counted: 0,
      target: 5,
    });
    expect(stepBar(running.episode)).toEqual({ value: 137, max: 400 });
    expect(stepBar(null)).toBeNull();
    expect(running.reason?.say).toEqual({
      key: "marvel0.run.reason.running",
      params: { episode: 1 },
    });
    expect(view("failed").reason?.say?.key).toBe(
      "marvel0.run.reason.exit_code",
    );
    expect(view("failed").reason?.say).toMatchObject({
      params: { code: expect.any(Number) },
    });
  });

  test("a reason whose facts are missing shows the server's sentence, not a bare placeholder", () => {
    const view_ = view("running", (doc) => {
      doc.episode = null;
    });
    expect(view_.reason?.say).toBeNull();
    expect(view_.reason?.text).toContain("running");
  });

  test("why an action is not offered is the backend's code, worded", () => {
    const gate = view("at_gate");
    expect(gate.actions.success.whyNot?.say).toEqual({
      key: "marvel0.run.why.state_not_allowed",
      params: { state: "at_gate" },
    });
    expect(gate.actions.enter.whyNot).toBeNull();
  });

  test("the start checks keep the backend's level; a warning never refuses", () => {
    const rows = view("preflight_failing").preflight;
    expect(rows.find((r) => r.code === "policy_ready")?.level).toBe("refuse");
    expect(rows.find((r) => r.code === "no_session")?.level).toBe("unknown");
    expect(rows.find((r) => r.code === "robot_server")?.level).toBe("warn");
    expect(rows.find((r) => r.code === "product")?.level).toBe("ok");
  });
});

describe("which key each state carries", () => {
  test("at the start gate only Enter and q (End at the gate); never s or p", () => {
    expect(allowed("at_gate")).toEqual(
      ["enter", "hold", "end_at_gate"].sort(
        (a, b) => ACTIONS.indexOf(a as never) - ACTIONS.indexOf(b as never),
      ),
    );
    expect(armedActions("at_gate")).toEqual(["enter"]);
  });

  test("running: success and failure; awaiting the label: success, failure and discard", () => {
    expect(armedActions("running")).toEqual(["success", "failure"]);
    expect(armedActions("awaiting_label")).toEqual([
      "success",
      "failure",
      "discard",
    ]);
  });

  test("homing, finishing, held, lost and the rest carry no armed key", () => {
    for (const state of [
      "idle",
      "starting",
      "homing",
      "finishing",
      "done",
      "failed",
      "held",
      "lost",
    ] as const)
      expect(armedActions(state)).toEqual([]);
  });

  test("a held client offers End and nothing that moves the arm", () => {
    expect(allowed("held")).toEqual(["end"]);
  });

  test("a challenge is asked for the purpose of the key, and hold, end and start have none of the keys'", () => {
    expect(purposeOf("enter")).toBe("enter");
    expect(purposeOf("end_at_gate")).toBe("end_at_gate");
    expect(purposeOf("exit_home")).toBe("exit_home");
    for (const id of ["success", "failure", "discard"] as const)
      expect(purposeOf(id)).toBe("label");
    expect(purposeOf("hold")).toBeNull();
    expect(purposeOf("end")).toBeNull();
  });

  test("the page has no name for the keys that run another task at the start gate", () => {
    // s and p at the client's start gate choose another task (2026-10-20).
    expect(ACTIONS).not.toContain("s" as never);
    expect(ACTIONS).not.toContain("p" as never);
    const code = readFileSync(join(import.meta.dir, "../run-api.ts"), "utf8");
    // The request bodies name actions; there is no field for a raw key.
    expect(code).not.toMatch(/\bkey:\s*"[sp]"/);
    expect(code).not.toMatch(/"[sp]"/);
  });
});

describe("hold", () => {
  test("on in every state in which the arm may move, whatever else is going on", () => {
    for (const state of [
      "starting",
      "at_gate",
      "homing",
      "running",
      "awaiting_label",
      "finishing",
      "lost",
    ] as const) {
      expect(mayMove(state)).toBe(true);
      expect(holdEnabled({ state, mayMove: false })).toBe(true);
    }
  });

  test("off when nothing runs, when the client is over, and before anything was read", () => {
    for (const state of [
      "idle",
      "preflight",
      "done",
      "failed",
      "held",
    ] as const)
      expect(holdEnabled({ state, mayMove: false })).toBe(false);
    expect(holdEnabled(null)).toBe(false);
  });

  test("the backend's own may_move turns it on too", () => {
    expect(holdEnabled({ state: "idle", mayMove: true })).toBe(true);
  });
});

describe("the phrases", () => {
  test("the start phrase names the group; the others are the backend's", () => {
    const read = view("idle_ready");
    expect(startPhrase(read, "my_group")).toBe("start client my_group");
    expect(runPhrase(read, "exit_home")).toBe("exit and go home");
    expect(runPhrase(read, "end")).toBe("end client run");
    expect(startPhrase(null, "g")).toBe("start client g");
  });
});
