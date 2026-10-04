import { describe, expect, test } from "bun:test";
import { designPageEnabled } from "../gate";

describe("design page gate", () => {
  test("open in development, closed in production unless LEVI_DESIGN_PAGE=1", () => {
    expect(designPageEnabled({ NODE_ENV: "development" })).toBe(true);
    expect(designPageEnabled({ NODE_ENV: "production" })).toBe(false);
    expect(
      designPageEnabled({ NODE_ENV: "production", LEVI_DESIGN_PAGE: "1" }),
    ).toBe(true);
    expect(
      designPageEnabled({ NODE_ENV: "production", LEVI_DESIGN_PAGE: "true" }),
    ).toBe(false);
  });
});
