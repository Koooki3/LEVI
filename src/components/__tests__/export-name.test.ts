import { describe, expect, test } from "bun:test";

import { exportName } from "../levi-api";

describe("exportName", () => {
  test("names the dataset, the kind and the UTC hour, nothing else", () => {
    const at = new Date("2026-09-22T09:41:07Z");
    expect(
      exportName(
        "local/stack_the_plates_of_same_color_together",
        "quality",
        at,
      ),
    ).toBe("stack_the_plates_of_same_color_together-quality-20260922T09.json");
  });

  test("keeps a Hub dataset readable without its organisation", () => {
    expect(
      exportName(
        "lerobot/aloha sim",
        "review",
        new Date("2026-01-02T03:00:00Z"),
      ),
    ).toBe("aloha_sim-review-20260102T03.json");
  });
});
