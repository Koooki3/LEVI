import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import { sessionKeys } from "../live-logic";
import { SessionsPanel } from "../session-panels";
import type { LiveSession } from "../types";

const session = (run_id?: string): LiveSession =>
  ({
    group: "pi05_fr3_all_state",
    task_folder: "pick_the_eggplant_in_the_blue_plate",
    state: "finished",
    run_id,
  }) as LiveSession;

describe("a list of sessions never repeats a key", () => {
  test("two runs of the same model and task folder get different keys", () => {
    const keys = sessionKeys([
      session("a"),
      session("b"),
      session("a"),
      session(),
    ]);
    expect(new Set(keys).size).toBe(4);
    expect(keys[0]).toContain("/a");
    expect(sessionKeys([session(), session()])[1]).toMatch(/#1$/);
  });

  test("both runs show as cards, and React logs no duplicate key", () => {
    const errors: unknown[][] = [];
    const original = console.error;
    console.error = (...args: unknown[]) => void errors.push(args);
    try {
      const html = renderToStaticMarkup(
        <SessionsPanel sessions={[session("run-1"), session("run-2")]} />,
      );
      expect(html).toContain("run-1");
      expect(html).toContain("run-2");
    } finally {
      console.error = original;
    }
    expect(errors.filter((e) => String(e[0]).includes("same key"))).toEqual([]);
  });
});
