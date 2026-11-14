import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import { Fr3Panel, SessionCard } from "../session-panels";
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
  renderToStaticMarkup(<SessionCard session={s} open onToggle={() => {}} />);

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

test("session statistics links keep the exact session route and are absent from detail cards", () => {
  const s = session({
    root: "/model-root",
    session_id: "session-exact",
    dataset: "model__task",
  });
  const out = html(s);
  expect(out).toContain("#session-statistics");
  expect(out).toContain(">Performance statistics</a>");
  const detail = renderToStaticMarkup(
    <SessionCard session={s} open detail onToggle={() => {}} />,
  );
  expect(detail).not.toContain("#session-statistics");
});

describe("the arm's state when it is the last one read", () => {
  const html = (
    stale: boolean,
    state: string,
    staleWhy?: "core" | "sessions",
  ) =>
    renderToStaticMarkup(
      <Fr3Panel
        fr3={{
          state: state as never,
          reasons: ["Cartesian reflex: joint 4"],
          current_errors: ["cartesian_reflex"],
        }}
        stale={stale}
        staleWhy={staleWhy}
      />,
    );
  test("a red light stays red, keeps its reasons and says it is the last known", () => {
    const out = html(true, "red", "core");
    expect(out).toContain("Last known: Red light");
    expect(out).toContain("pg-live-section fault");
    expect(out).toContain("Cartesian reflex: joint 4");
    expect(out).not.toContain("Unknown");
  });
  test("an old OK is never drawn as OK, and the text names why it is old", () => {
    const core = html(true, "ok", "core");
    expect(core).toContain("Unknown");
    expect(core).not.toContain(">OK<");
    expect(core).toContain("cannot reach the LEVI core");
    // Only the session list failing: the core answers, so it must not be blamed.
    const sessions = html(true, "ok", "sessions");
    expect(sessions).not.toContain("cannot reach the LEVI core");
    expect(sessions).toContain("session list is not answering");
  });
  test("a current state is drawn as before", () => {
    expect(html(false, "ok")).toContain("No error reported by the robot.");
  });
});
