import { click, render, setupDom } from "../../ds/__tests__/dom";
import { describe, expect, test } from "bun:test";
import { ReportBlock, ReportContext } from "../report-blocks";

setupDom();

const GLYPHS = /[▲▼↑↓↕]/;

describe("report table block", () => {
  const source = JSON.stringify({
    data: {
      columns: [
        { key: "name", label: "Name" },
        { key: "score", label: "Score" },
      ],
      rows: [
        { name: "b", score: 2 },
        { name: "a", score: 3 },
      ],
    },
  });

  test("sort state is aria-sort on the header, shown by icons not glyphs", async () => {
    const { host } = await render(
      <ReportBlock language="levi-table" source={source} />,
    );
    const heads = [...host.querySelectorAll("th")];
    expect(heads.map((th) => th.getAttribute("aria-sort"))).toEqual([
      "none",
      "none",
    ]);
    expect(host.textContent).not.toMatch(GLYPHS);
    // Each header shows an icon, never a text arrow.
    expect(host.querySelectorAll("th button svg").length).toBe(2);

    await click(heads[1].querySelector("button"));
    expect(
      [...host.querySelectorAll("th")].map((th) =>
        th.getAttribute("aria-sort"),
      ),
    ).toEqual(["none", "ascending"]);
    expect(
      [...host.querySelectorAll("tbody tr")].map((tr) => tr.textContent),
    ).toEqual(["b2", "a3"]);

    await click(host.querySelectorAll("th")[1].querySelector("button"));
    expect(host.querySelectorAll("th")[1].getAttribute("aria-sort")).toBe(
      "descending",
    );
    expect(host.textContent).not.toMatch(GLYPHS);
  });

  test("the sort hint is a tooltip, not a title attribute", async () => {
    const { host } = await render(
      <ReportBlock language="levi-table" source={source} />,
    );
    const button = host.querySelector("th button");
    expect(button?.hasAttribute("title")).toBe(false);
    expect(button?.getAttribute("aria-describedby")).toBeTruthy();
    expect(host.querySelector('[role="tooltip"]')?.textContent).toBe(
      "Sort by this column",
    );
  });
});

describe("report metrics block", () => {
  test("the delta direction is an icon with the number, not a text arrow", async () => {
    const source = JSON.stringify({
      data: [
        { label: "Up", value: 3, baseline: 2 },
        { label: "Down", value: 1, baseline: 2 },
        { label: "Same", value: 2, baseline: 2 },
        { label: "None", value: 2 },
      ],
    });
    const { host } = await render(
      <ReportBlock language="levi-metrics" source={source} />,
    );
    expect(host.textContent).not.toMatch(GLYPHS);
    expect(host.textContent).not.toContain("=");
    const deltas = [...host.querySelectorAll(".lr-delta")];
    expect(deltas.length).toBe(3);
    for (const delta of deltas) expect(delta.querySelector("svg")).toBeTruthy();
    expect(host.querySelectorAll(".lr-metric")[3].querySelector("svg")).toBe(
      null,
    );
  });
});

describe("report progress block", () => {
  test("a state is an icon plus its name", async () => {
    const status = {
      workstreams: [
        { id: "a", title: "Alpha", state: "running", progress: 0.5 },
        { id: "b", title: "Beta", state: "blocked", progress: 0.1 },
      ],
    };
    const { host } = await render(
      <ReportContext.Provider
        value={{
          status: status as never,
          lang: "en",
          now: Date.now(),
        }}
      >
        <ReportBlock language="levi-progress" source="{}" />
      </ReportContext.Provider>,
    );
    const states = [...host.querySelectorAll(".lr-state")];
    expect(states.map((s) => s.textContent)).toEqual(["Running", "Blocked"]);
    for (const state of states) expect(state.querySelector("svg")).toBeTruthy();
    expect(host.querySelector(".lr-state-dot")).toBeNull();
  });
});
