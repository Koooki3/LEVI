import { describe, expect, test } from "bun:test";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import {
  SEQUENCE_STEP_STATES,
  actView,
  type SequenceAct,
} from "../stack-act-logic";
import {
  STEP_TONE,
  failedStep,
  sequenceEnded,
  shownState,
  stopSummary,
} from "../stack-sequence-logic";
import { stackDoc } from "./ctl-fixtures";

const KNOWN = new Set(Object.keys(en));
const seqOf = (
  name: string,
  change?: (seq: Record<string, any>) => void, // eslint-disable-line @typescript-eslint/no-explicit-any
): SequenceAct =>
  actView(
    stackDoc(name, (doc) => change?.(doc.act.sequence)),
    KNOWN,
  )!.sequence;

describe("a step the sequence never reached", () => {
  test("pending while the sequence goes on, not reached once it has ended without it", () => {
    const running = seqOf("stop_all_running");
    expect(running.steps.map((s) => shownState(running, s))).toEqual([
      "done",
      "running",
      "pending",
    ]);
    const failed = seqOf("stop_all_failed");
    expect(sequenceEnded(failed)).toBe(true);
    expect(failed.steps.map((s) => shownState(failed, s))).toEqual([
      "done",
      "failed",
      "not_reached",
    ]);
    const cancelled = seqOf("stop_all_cancelled");
    expect(
      cancelled.steps.some((s) => shownState(cancelled, s) === "pending"),
    ).toBe(false);
  });

  test("a sequence that is still on, waiting for the person included, has no step that was not reached", () => {
    for (const name of ["sequence_running", "sequence_waiting_b"]) {
      const seq = seqOf(name, (raw) => {
        raw.steps = [
          { step: "monitor", terminal: "m", state: "done" },
          { step: "arm", terminal: "a", state: "done" },
          { step: "b", terminal: "b", state: "pending" },
        ];
      });
      expect(sequenceEnded(seq)).toBe(false);
      expect(seq.steps.map((s) => shownState(seq, s))).toEqual([
        "done",
        "done",
        "pending",
      ]);
    }
  });

  test("a step the backend still calls pending after the sequence ended reads as not reached, whatever the backend sent", () => {
    const seq = seqOf("stop_all_failed", (raw) => {
      raw.steps[2].state = "pending";
    });
    expect(seq.steps.map((s) => shownState(seq, s))).toEqual([
      "done",
      "failed",
      "not_reached",
    ]);
  });

  test("every step state has a tone and a word, for a start and for a stop, in both languages", () => {
    expect(SEQUENCE_STEP_STATES).toContain("not_reached");
    for (const state of SEQUENCE_STEP_STATES) {
      expect(STEP_TONE[state]).toBeTruthy();
      for (const family of ["step_state", "start_step_state"]) {
        const key = `marvel0.ctl.seq.${family}.${state}`;
        expect([key, key in en]).toEqual([key, true]);
        expect([key, key in zh]).toEqual([key, true]);
      }
    }
    expect(en["marvel0.ctl.seq.start_step_state.not_reached"]).toBe(
      "Not reached",
    );
  });
});

describe("where a sequence failed and what it did", () => {
  test("the failed step is the one the backend marked; a sequence that did not fail has none", () => {
    expect(failedStep(seqOf("stop_all_failed"))?.step).toBe("stop_a");
    expect(failedStep(seqOf("stop_all_running"))).toBeNull();
    expect(failedStep(seqOf("stop_all_cancelled"))).toBeNull();
    // A failed sequence whose steps name none (an older backend): no step to name.
    expect(
      failedStep(
        seqOf("stop_all_failed", (raw) => {
          raw.steps = [];
        }),
      ),
    ).toBeNull();
  });

  test("a finished stop is told from the steps that were done or skipped, never from the plan", () => {
    expect(stopSummary(seqOf("stop_all_done"))).toEqual({
      stopped: ["C", "A", "B"],
      already: [],
    });
    expect(
      stopSummary(
        seqOf("stop_all_done", (raw) => {
          raw.steps[0].state = "skipped";
          raw.steps[2].state = "skipped";
        }),
      ),
    ).toEqual({ stopped: ["A"], already: ["C", "B"] });
    // A step that failed or was never reached is neither.
    expect(stopSummary(seqOf("stop_all_failed"))).toEqual({
      stopped: ["C"],
      already: [],
    });
  });

  test("a start's steps keep the terminal the backend gives them (the FCI check, the marker and the wait belong to A)", () => {
    const seq = seqOf("sequence_running", (raw) => {
      raw.steps = [
        { step: "monitor", terminal: "m", state: "done" },
        { step: "marker", terminal: "a", state: "done" },
        { step: "wait", terminal: "a", state: "running" },
        { step: "c", terminal: "c", state: "pending" },
      ];
    });
    expect(seq.steps.map((s) => [s.step, s.terminal, s.state])).toEqual([
      ["monitor", "M", "done"],
      ["marker", "A", "done"],
      ["wait", "A", "running"],
      ["c", "C", "pending"],
    ]);
  });
});
