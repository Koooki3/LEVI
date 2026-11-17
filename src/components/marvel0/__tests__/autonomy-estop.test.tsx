import { afterEach, describe, expect, test } from "bun:test";
import { click, setupDom, waitFor } from "@/components/ds/__tests__/dom";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import { $, dialog, keyButton, openStep, text } from "./autonomy-helpers";
import { readyDoc, runDoc } from "./autonomy-fixtures";
import { keyChecks, requiredAttest } from "../autonomy-logic";

// L2-STEP5-SIMPLIFY-1117, item 6: "a hand on the e-stop" is one standing line of
// the step, asked nowhere again: no tick in the start window, in a key's window
// or in the forced end, and no banner repeating it in them.

setupDom();
afterEach(() => {
  try {
    localStorage.clear();
  } catch {
    // no storage in this DOM
  }
});

const E = en as Record<string, string>;
const Z = zh as Record<string, string>;
// "a hand on the e-stop" in any wording (the honest statement that the gates are
// not the barrier names the physical emergency stop once, without asking a hand).
const ESTOP = /hand[^.]*e-stop|e-stop[^.]*hand|keep a hand/i;
const allMet = () =>
  readyDoc("prepared_ready", (doc) => {
    for (const row of doc.readiness) row.level = "ok";
  });

describe("the standing line", () => {
  test.each(["idle", "await_home_o", "forward_running", "await_release"])(
    "the step says it, in both languages, whatever D is doing: %s",
    async (name) => {
      const f = await openStep({ run: runDoc(name), ready: allMet() });
      expect(text($(f.host, "marvel0-autonomy-honest"))).toMatch(ESTOP);
      const zh2 = await openStep({
        run: runDoc(name),
        ready: allMet(),
        zh: true,
      });
      expect(text($(zh2.host, "marvel0-autonomy-honest"))).toContain("急停");
    },
  );
});

describe("nothing asks for it again", () => {
  test("no tick of any key or of the forced end names it, and none carries its code", () => {
    for (const id of ["execute", "execute_variant", "end"] as const) {
      for (const check of keyChecks(id)) {
        expect([id, check.code]).not.toEqual([id, "estop_hand"]);
        expect([id, check.code, E[check.label]]).not.toEqual([
          id,
          check.code,
          expect.stringMatching(ESTOP),
        ]);
        expect(Z[check.label]).not.toContain("急停");
      }
      expect(requiredAttest(id)).not.toContain("estop_hand");
    }
    expect(E["marvel0.autonomy.dcheck.scene_ready"]).not.toMatch(ESTOP);
    expect(Z["marvel0.autonomy.dcheck.scene_ready"]).not.toContain("急停");
    expect(Object.keys(E)).not.toContain("marvel0.autonomy.ack.estop");
    expect(Object.keys(E)).not.toContain("marvel0.autonomy.estop_line");
  });

  test("Start says what its click states beside the button, with no e-stop sentence, and has no window", async () => {
    const f = await openStep({ ready: allMet() });
    const says = text($(f.host, "marvel0-autonomy-start-says"));
    expect(says).not.toMatch(ESTOP);
    expect(
      f.host.querySelectorAll("input[type=checkbox]:not([role=switch])"),
    ).toHaveLength(0);
    await click($(f.host, "marvel0-autonomy-start"));
    expect(dialog()).toBeNull();
  });

  test("the window of a key that moves the arm, and of the long start, carry none", async () => {
    for (const [name, key] of [
      ["forward_running", "verdict_success"],
      ["await_phrase_variant", "execute_variant"],
    ] as const) {
      const f = await openStep({ run: runDoc(name) });
      await waitFor(() => !keyButton(f.host, key)!.disabled, {
        label: `${key} on`,
      });
      await click(keyButton(f.host, key));
      expect([name, text(dialog()!)]).not.toEqual([
        name,
        expect.stringMatching(ESTOP),
      ]);
    }
  });
});
