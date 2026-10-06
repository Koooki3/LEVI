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
