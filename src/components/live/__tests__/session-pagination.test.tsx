import { click, render, setupDom } from "@/components/ds/__tests__/dom";
import { describe, expect, mock, test } from "bun:test";
import { SessionsPanel } from "../session-panels";
import type { LiveSession } from "../types";
mock.module("next/link", () => ({
  default: ({ children, ...props }: Record<string, unknown>) => (
    <a {...(props as object)}>{children as never}</a>
  ),
}));
setupDom();
const sessions: LiveSession[] = Array.from({ length: 23 }, (_, i) => ({
  root: "/fixture",
  group: "model",
  task_folder: "task",
  run_id: "run-" + i,
  session_id: "session-" + i,
  state: "finished",
  updated_at: i,
}));
function next(host: HTMLElement) {
  return [...host.querySelectorAll("button")].find(
    (b) => b.textContent === "Next",
  )!;
}
describe("dense session pagination", () => {
  test("all 23 sessions remain reachable without rendering an endless list", async () => {
    const { host } = await render(<SessionsPanel sessions={sessions} />);
    const seen = new Set<string>();
    for (let i = 0; i < 4; i++) {
      const cards = host.querySelectorAll(".pg-live-list-row");
      expect(cards.length).toBeLessThanOrEqual(6);
      cards.forEach((c) =>
        seen.add(c.querySelector(".pg-live-row-title > span")!.textContent!),
      );
      if (i < 3) await click(next(host));
    }
    expect(seen.size).toBe(23);
    expect(next(host).disabled).toBe(true);
  });
  test("a shrinking polling result stays on a valid page", async () => {
    const { host, rerender } = await render(
      <SessionsPanel sessions={sessions} />,
    );
    await click(next(host));
    await rerender(<SessionsPanel sessions={sessions.slice(0, 2)} />);
    expect(host.querySelectorAll(".pg-live-list-row").length).toBe(2);
    expect(host.querySelector(".pg-live-pagination")).toBeNull();
  });
});
