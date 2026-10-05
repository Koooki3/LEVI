import { describe, expect, test } from "bun:test";
import { readFileSync } from "fs";
import { join } from "path";
import {
  PANEL_DEFAULT,
  PANEL_MIN,
  clampPanelWidth,
  panelAria,
  panelMax,
  panelWidthForKey,
} from "../panel-width";

describe("Agent Workbench drawer width", () => {
  test("the separator states min, max and the current width", () => {
    expect(panelAria(null, 1440)).toEqual({
      "aria-valuemin": PANEL_MIN,
      "aria-valuemax": 1408,
      "aria-valuenow": PANEL_DEFAULT,
    });
    expect(panelAria(700, 1440)["aria-valuenow"]).toBe(700);
    // A stored width wider than this window is reported as what it gets.
    expect(panelAria(2000, 1000)["aria-valuenow"]).toBe(968);
    expect(panelMax(300)).toBe(PANEL_MIN);
  });

  test("keys: ← widens, → narrows, Home and End go to the ends", () => {
    expect(panelWidthForKey("ArrowLeft", false, null, 1440)).toBe(514);
    expect(panelWidthForKey("ArrowRight", false, 500, 1440)).toBe(476);
    expect(panelWidthForKey("ArrowLeft", true, 500, 1440)).toBe(580);
    expect(panelWidthForKey("ArrowRight", false, 370, 1440)).toBe(PANEL_MIN);
    expect(panelWidthForKey("Home", false, 900, 1440)).toBe(PANEL_MIN);
    expect(panelWidthForKey("End", false, 900, 1440)).toBe(1408);
    expect(panelWidthForKey("a", false, 900, 1440)).toBeNull();
    expect(clampPanelWidth(5000, 800)).toBe(768);
  });

  test("the drawer's resize handle carries the values", () => {
    const code = readFileSync(
      join(import.meta.dir, "../../agent-workbench.tsx"),
      "utf8",
    );
    const grip = code.slice(code.indexOf('className="levi-agent-grip"'));
    expect(grip.slice(0, 1500)).toContain("{...panelAria(width, viewport)}");
    expect(grip.slice(0, 1500)).toContain("panelWidthForKey(");
  });
});
