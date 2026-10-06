import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import { SessionsPanel } from "../session-panels";
import type { LiveSession } from "../types";

const session = (over: Partial<LiveSession> = {}): LiveSession => ({
  group: "pi05_fr3_all_state",
  task_folder: "pick_the_eggplant",
  state: "waiting_reset",
  run_id: "20261006-101500",
  levi_enabled: true,
  ...over,
});
const html = (s: LiveSession) =>
  renderToStaticMarkup(<SessionsPanel sessions={[s]} />);

describe("a dual-label session", () => {
  test("is marked, and its last episode's outcome is the operator's", () => {
    const out = html(
      session({
        label_mode: "dual_label",
        reset_wait_s: null,
        last_episode: { outcome: "failure", steps: 400 },
      }),
    );
    expect(out).toContain("Dual labels");
    expect(out).toContain("Operator label");
    expect(out).toContain('class="pg-live-optag"');
    expect(out).toContain("Failure");
  });
  test("waits for the operator, not a countdown", () => {
    const out = html(session({ label_mode: "dual_label", reset_wait_s: null }));
    expect(out).toContain("the operator starts the next episode (Enter)");
  });
  test("while the operator labels, the wait says so", () => {
    const out = html(
      session({
        label_mode: "dual_label",
        reset_wait_s: null,
        reason: "operator labelling episode 3",
      }),
    );
    expect(out).toContain("the operator is labelling the episode");
    expect(out).not.toContain("the operator starts the next episode");
    const reset = html(
      session({
        label_mode: "dual_label",
        reset_wait_s: null,
        reason:
          "waiting for the operator: reset the scene, Enter starts episode 4",
      }),
    );
    expect(reset).toContain("the operator starts the next episode (Enter)");
  });
  test("a discarded episode reads Discarded, without the operator chip", () => {
    for (const label_mode of ["dual_label", "unattended"] as const) {
      const out = html(
        session({
          label_mode,
          reset_wait_s: null,
          last_episode: { outcome: "discarded", steps: 6 },
        }),
      );
      expect(out).toContain("Discarded · 6");
      expect(out).not.toContain("pg-live-optag");
    }
  });
  test("an unattended or older session is shown as before", () => {
    for (const s of [
      session({ label_mode: "unattended", reset_wait_s: 10 }),
      session({ reset_wait_s: 10 }),
    ]) {
      const out = html({ ...s, last_episode: { outcome: "unlabeled" } });
      expect(out).not.toContain("Dual labels");
      expect(out).not.toContain("pg-live-optag");
      expect(out).not.toContain("the operator starts the next episode");
      expect(out).toContain("10 s");
    }
    // Not waiting: no wait at all, whatever the mode.
    const running = html(
      session({
        state: "running",
        label_mode: "dual_label",
        reset_wait_s: null,
      }),
    );
    expect(running).not.toContain("the operator starts the next episode");
  });
});
