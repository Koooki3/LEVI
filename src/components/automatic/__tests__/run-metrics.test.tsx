import { render, setupDom } from "@/components/ds/__tests__/dom";
import { describe, expect, test } from "bun:test";
import { RunMetrics } from "../run-metrics";

setupDom();

const report = {
  reset_mode: "human_assisted",
  comparable: ["automation.unplanned", "turnaround"],
  mode_specific: ["automation.planned"],
  truth_labels: { task_outcome: 1, initial_state: 0 },
  automation: {
    planned: 1,
    unplanned: 0,
    unplanned_share: { n: 0, of: 0, rate: null, wilson95: null },
  },
  turnaround: { scene_ms: { mean: 450, n: 2 } },
  agreement: {
    judged: 3,
    agreement: { n: 2, of: 3, rate: 0.67, wilson95: [0.2, 0.94] },
  },
};

describe("the metrics section (P5)", () => {
  test("groups the metrics under plain headings, with the report's keys folded away", async () => {
    const { host } = await render(<RunMetrics report={report} blind={false} />);
    const headings = Array.from(host.querySelectorAll("h3")).map(
      (h) => h.textContent,
    );
    expect(headings).toEqual([
      "Run overview",
      "Interventions and turnaround",
      "Consistency with your labels",
    ]);
    // The grouped tables hold no raw key; the fold below holds them all.
    const grouped = Array.from(host.querySelectorAll(".ar-metric-group")).map(
      (n) => n.textContent ?? "",
    );
    expect(grouped.join(" ")).not.toContain("turnaround.scene_ms");
    expect(grouped.join(" ")).toContain("Scene check time · average");
    expect(grouped.join(" ")).toContain("450 ms");
    expect(grouped.join(" ")).toContain("No data yet");
    const fold = host.querySelector("details.ar-raw")!;
    expect(fold.textContent).toContain("turnaround.scene_ms.mean");
    expect(fold.textContent).toContain("All fields");
    // Comparable fields keep their label.
    expect(grouped.join(" ")).toContain("comparable");
    expect(grouped.join(" ")).toContain("mode-specific");
  });
  test("while the last episode is unlabelled the consistency group is absent everywhere", async () => {
    const { host } = await render(<RunMetrics report={report} blind={true} />);
    const text = host.textContent ?? "";
    expect(text).not.toContain("Consistency");
    expect(text).not.toContain("agreement");
  });
});
