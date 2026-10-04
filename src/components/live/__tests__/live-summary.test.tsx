import { render, setupDom } from "../../ds/__tests__/dom";
import { describe, expect, test } from "bun:test";
import { LiveSummaryBar, liveSummary } from "../live-summary";
import type { DatasetRow, LiveSession } from "../types";

setupDom();

const row = (
  done: number,
  episodes: number,
  last_error?: string,
  last_processed_at?: number,
) =>
  ({
    episodes,
    pending: 0,
    annotating: 0,
    done,
    failed: 0,
    last_error,
    last_processed_at,
  }) as DatasetRow;

describe("live summary row", () => {
  test("counts running sessions, labelled of finished, the latest error", () => {
    const sessions = [
      { state: "running" },
      { state: "finished" },
      { state: "running" },
    ] as LiveSession[];
    const summary = liveSummary(
      sessions,
      { a: row(3, 5), b: row(2, 2, "vLLM down", 100) },
      null,
    );
    expect(summary).toEqual({
      running: 2,
      sessions: 3,
      labelled: 5,
      finished: 7,
      lastError: "vLLM down",
      lastErrorAt: 100,
    });
    expect(liveSummary([], {}, { last_error: "core error" }).lastError).toBe(
      "core error",
    );
    expect(liveSummary([], {}, undefined).lastError).toBe("");
  });

  test("the most recent error is the newest by time, not the first dataset", () => {
    const rows = {
      first: row(1, 1, "old failure", 100),
      second: row(1, 1, "new failure", 300),
      third: row(1, 1, "middle failure", 200),
    };
    expect(liveSummary([], rows, null).lastError).toBe("new failure");
    // An error event of the service newer than every dataset wins.
    const service = {
      last_error: "gate stuck",
      events: [
        { time: 50, level: "error", text: "gate stuck since start" },
        { time: 400, level: "error", text: "vLLM did not start" },
        { time: 500, level: "info", text: "an info line is not an error" },
      ],
    };
    const summary = liveSummary([], rows, service);
    expect(summary.lastError).toBe("vLLM did not start");
    expect(summary.lastErrorAt).toBe(400);
    // An error without a time loses to one with a time.
    expect(
      liveSummary(
        [],
        { a: row(1, 1, "no time"), b: row(1, 1, "timed", 5) },
        null,
      ).lastError,
    ).toBe("timed");
  });

  test("renders the three figures with words, not colour alone", async () => {
    const { host } = await render(
      <LiveSummaryBar
        summary={{
          running: 1,
          sessions: 2,
          labelled: 41,
          finished: 50,
          lastError: "",
          lastErrorAt: null,
        }}
      />,
    );
    const text = host.textContent!;
    expect(text).toContain("Running sessions");
    expect(text).toContain("1 / 2");
    expect(text).toContain("41 / 50");
    expect(text).toContain("none");
    expect(host.querySelector(".ds-breathe")).not.toBeNull();
  });
});
