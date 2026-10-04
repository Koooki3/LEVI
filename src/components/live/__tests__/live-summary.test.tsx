import { render, setupDom } from "../../ds/__tests__/dom";
import { describe, expect, test } from "bun:test";
import { LiveSummaryBar, liveSummary } from "../live-summary";
import type { DatasetRow, LiveSession } from "../types";

setupDom();

const row = (done: number, episodes: number, last_error?: string) =>
  ({
    episodes,
    pending: 0,
    annotating: 0,
    done,
    failed: 0,
    last_error,
  }) as DatasetRow;

describe("live summary row", () => {
  test("counts running sessions, labelled of finished, the latest error", () => {
    const sessions = [
      { state: "running" },
      { state: "finished" },
      { state: "running" },
    ] as LiveSession[];
    expect(
      liveSummary(sessions, { a: row(3, 5), b: row(2, 2, "vLLM down") }, ""),
    ).toEqual({
      running: 2,
      sessions: 3,
      labelled: 5,
      finished: 7,
      lastError: "vLLM down",
    });
    expect(liveSummary([], {}, "core error").lastError).toBe("core error");
    expect(liveSummary([], {}, undefined).lastError).toBe("");
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
