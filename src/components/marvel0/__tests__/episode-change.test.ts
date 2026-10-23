import { describe, expect, test } from "bun:test";
import en from "@/i18n/en.json";
import { bodyWithNextPrompt, confirmedRunExit } from "../episode-change";
import { runView } from "../run-logic";
import { runDoc } from "./ctl-fixtures";

const known = new Set(Object.keys(en));

function view(over: Record<string, unknown> = {}) {
  const doc = runDoc("done");
  doc.run = {
    run_id: "run-1",
    phase: "ended",
    lock_released: true,
    form: { prompt: "old", model_id: "model" },
    ...over,
  };
  return runView(doc, known)!;
}

describe("changing the next episode instruction", () => {
  test("requires same identity, ended phase, released lock, done and not moving", () => {
    expect(confirmedRunExit(view(), "run-1")).toBe(true);
    expect(confirmedRunExit(view(), "other-run")).toBe(false);
    expect(confirmedRunExit(view({ phase: "started" }), "run-1")).toBe(false);
    expect(confirmedRunExit(view({ lock_released: false }), "run-1")).toBe(
      false,
    );
    const failed = runDoc("failed");
    failed.run = {
      run_id: "run-1",
      phase: "ended",
      lock_released: true,
    };
    expect(confirmedRunExit(runView(failed, known)!, "run-1")).toBe(false);
    const moving = runDoc("done");
    moving.may_move = true;
    moving.run = {
      run_id: "run-1",
      phase: "ended",
      lock_released: true,
    };
    expect(confirmedRunExit(runView(moving, known)!, "run-1")).toBe(false);
  });

  test("copies the saved body and changes only prompt", () => {
    const saved = {
      model_id: "model",
      prompt: "old",
      max_steps: 500,
      rtc: true,
      control_hz: 20,
    };
    expect(bodyWithNextPrompt(saved, "new instruction")).toEqual({
      ...saved,
      prompt: "new instruction",
    });
    expect(saved.prompt).toBe("old");
  });
});
