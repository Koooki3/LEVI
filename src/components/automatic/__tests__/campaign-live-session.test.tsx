import { describe, expect, test, mock } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
mock.module("next/link", () => ({
  default: ({ children, ...props }: Record<string, unknown>) => (
    <a {...(props as object)}>{children as never}</a>
  ),
}));
const { CampaignSessionLink } = await import("../campaign-live-session");
const session = {
  root: "/fixture",
  group: "model",
  task_folder: "task",
  session_id: "sid",
  run_id: "rid",
  state: "running",
  dataset: null,
};
describe("campaign's client association", () => {
  test("old server has no invented association", () =>
    expect(renderToStaticMarkup(<CampaignSessionLink />)).toBe(""));
  test("linked and ended sessions use the shared detail route", () => {
    for (const status of ["linked", "ended"] as const) {
      const out = renderToStaticMarkup(
        <CampaignSessionLink value={{ status, segment: 1, session }} />,
      );
      expect(out).toContain("/live/sessions/");
      expect(out).toContain("Session details");
    }
  });
  test("waiting, ambiguous, mismatch and unavailable never link even with a candidate", () => {
    for (const status of [
      "waiting",
      "ambiguous",
      "mismatch",
      "unavailable",
    ] as const) {
      const out = renderToStaticMarkup(
        <CampaignSessionLink value={{ status, segment: 1, session }} />,
      );
      expect(out).not.toContain("/live/sessions/");
    }
  });
});
