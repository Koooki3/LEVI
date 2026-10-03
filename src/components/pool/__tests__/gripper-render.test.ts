import { describe, expect, test } from "bun:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { PoolWarnings } from "../composition-panel";
import { GripperMix } from "../task-pick";
import type { PoolWarning } from "../types";

describe("gripper parts render", () => {
  test("the composition lists each gripper with its count", () => {
    const html = renderToStaticMarkup(
      createElement(GripperMix, {
        grippers: { robotiq_2f85: 1200, franka_hand: 30 },
      }),
    );
    expect(html).toContain("Robotiq 2F-85 1,200");
    expect(html).toContain("Franka Hand 30");
    expect(html.toLowerCase()).not.toContain("no gripper is recorded");
  });

  test("episodes whose gripper was declared are counted", () => {
    const html = renderToStaticMarkup(
      createElement(GripperMix, {
        grippers: { robotiq_2f85: 1628 },
        declared: 1628,
      }),
    );
    expect(html).toContain(
      "1,628 episode(s): the gripper comes from a declaration",
    );
    expect(
      renderToStaticMarkup(
        createElement(GripperMix, { grippers: { robotiq_2f85: 3 } }),
      ),
    ).not.toContain("declaration");
  });

  test("only unknown grippers say so", () => {
    const html = renderToStaticMarkup(
      createElement(GripperMix, { grippers: { unknown: 7 } }),
    );
    expect(html).toContain("Unknown gripper 7");
    expect(html).toContain("No gripper is recorded for these sources");
    expect(
      renderToStaticMarkup(createElement(GripperMix, { grippers: {} })),
    ).toBe("");
  });

  test("a mixed selection shows as a blocking note", () => {
    const warning: PoolWarning = {
      code: "mixed_gripper",
      blocking: true,
      problem: "mixed_known",
      message: "",
      counts: { robotiq_2f85: 5, franka_hand: 2 },
    };
    const html = renderToStaticMarkup(
      createElement(PoolWarnings, { warnings: [warning] }),
    );
    expect(html).toContain("Blocks export");
    expect(html).toContain("mixes grippers: Robotiq 2F-85 5 · Franka Hand 2");
  });
});
