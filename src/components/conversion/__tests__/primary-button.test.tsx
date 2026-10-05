import { render, setupDom } from "../../ds/__tests__/dom";
import { describe, expect, test } from "bun:test";
import { ConversionWizard } from "../conversion-wizard";
import { TargetCards } from "../target-cards";
import type { TargetCompatibility } from "../types";

setupDom();

const target = (id: string): TargetCompatibility => ({
  target: id,
  label: id,
  status: "supported",
  reasons: [],
  solutions: [],
  defaults: {},
});

describe("one primary button per screen (conversion)", () => {
  test("the chosen export is pressed and outlined, not a primary button", async () => {
    const { host } = await render(
      <TargetCards
        targets={[target("lerobot_v21"), target("recap_value")]}
        selected="lerobot_v21"
        onSelect={() => {}}
        onSolution={() => {}}
      />,
    );
    const buttons = Array.from(host.querySelectorAll("button"));
    expect(buttons.map((b) => b.getAttribute("aria-pressed"))).toEqual([
      "true",
      "false",
    ]);
    expect(host.querySelector(".ds-btn--primary")).toBeNull();
  });

  test("before an inspection, Inspect input is the wizard's only primary", async () => {
    const { host } = await render(
      <ConversionWizard
        jobs={[]}
        refresh={() => Promise.resolve()}
        available
        source=""
        onSourceChange={() => {}}
      />,
    );
    const primaries = host.querySelectorAll(".ds-btn--primary");
    expect(primaries.length).toBe(1);
    expect(primaries[0].textContent).toContain("Inspect input");
  });
});
