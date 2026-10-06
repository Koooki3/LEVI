import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import { AgentVsOperator, DatasetCard } from "../dataset-card";
import type { AgreementSummary, DatasetDetail, DatasetRow } from "../types";

const summary = (over: AgreementSummary = {}): AgreementSummary => ({
  pairs: 10,
  judged: 7,
  agree: 5,
  rate: 0.714,
  false_success: { n: 1, of: 5, rate: 0.2, wilson95: [0.036, 0.624] },
  missed_success: { n: 2, of: 4, rate: 0.5, wilson95: [0.15, 0.85] },
  undecided: 2,
  no_agent: 1,
  operator_success_rate: 0.5,
  agent_success_rate: 0.333,
  ...over,
});

describe("agent vs operator on a dataset", () => {
  test("nothing until the operator labelled an episode success or failure", () => {
    expect(
      renderToStaticMarkup(
        <AgentVsOperator agreement={summary({ pairs: 0 })} />,
      ),
    ).toBe("");
    expect(
      renderToStaticMarkup(<AgentVsOperator agreement={undefined} />),
    ).toBe("");
  });
  test("the counts and rates, with both labels named", () => {
    const html = renderToStaticMarkup(
      <AgentVsOperator agreement={summary()} />,
    );
    expect(html).toContain("Agent vs operator");
    expect(html).toContain("Operator label (ground truth)");
    expect(html).toContain("agent label (automatic, unreviewed)");
    for (const part of [
      "operator labelled success or failure 10",
      "agree 5/7 (71%)",
      "false success 1/5",
      "missed success 2/4",
      "agent undecided 2",
      "no agent verdict yet 1",
      "success rate: operator / agent 50% / 33%",
    ])
      expect(html).toContain(part);
    // The count includes the episode with no agent verdict yet: it is not
    // called "both labels".
    expect(html).not.toContain("both labels");
  });
});

const row: DatasetRow = {
  episodes: 2,
  pending: 0,
  annotating: 0,
  done: 2,
  failed: 0,
};
const card = (detail: Partial<DatasetDetail>) =>
  renderToStaticMarkup(
    <DatasetCard
      name="pi05__plates"
      row={row}
      entry={{
        data: {
          enabled: true,
          name: "pi05__plates",
          demos: [{ demo: "demo_0000", state: "done" }],
          ...detail,
        },
        error: "",
        at: 0,
        signature: "",
      }}
      fault={null}
      open={false}
      onToggle={() => {}}
      workerPhase={null}
      filter="latest"
      onFilter={() => {}}
      nowSeconds={0}
      onChanged={() => {}}
    />,
  );

describe("the time segments line", () => {
  test("says the service runs the release review alone", () => {
    expect(card({ pipeline: { temporal: false } })).toContain(
      "off: release review only",
    );
  });
  test("is as before otherwise", () => {
    for (const html of [card({ pipeline: { temporal: true } }), card({})]) {
      expect(html).not.toContain("off: release review only");
      expect(html).toContain("none committed yet");
    }
  });
  test("the agreement block follows the dataset's own figures", () => {
    expect(card({ agreement: summary() })).toContain("Agent vs operator");
    expect(card({ agreement: summary({ pairs: 0 }) })).not.toContain(
      "Agent vs operator",
    );
  });
});
