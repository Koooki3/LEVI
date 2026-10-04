import { describe, expect, test } from "bun:test";
import { readFileSync } from "fs";
import { join } from "path";
import { parseRgb } from "../agent-object-tool";

describe("object tool mask colour comes from the data palette", () => {
  test("parses the resolved CSS colour", () => {
    expect(parseRgb("rgb(0, 131, 0)")).toEqual([0, 131, 0]);
    expect(parseRgb("rgba(57, 135, 229, 0.5)")).toEqual([57, 135, 229]);
    expect(parseRgb("rgb(1 2 3)")).toEqual([1, 2, 3]);
    expect(parseRgb("transparent")).toBeNull();
  });
  test("no hard-coded lime, no lint exemption; the CSS names --ds-data-6", () => {
    const code = readFileSync(
      join(import.meta.dir, "../agent-object-tool.tsx"),
      "utf8",
    );
    expect(code).not.toContain("#bee855");
    expect(code).not.toContain("190, 232, 85");
    expect(code).not.toContain("eslint-disable-next-line no-restricted-syntax");
    const css = readFileSync(
      join(import.meta.dir, "../pages-ui/agent-content.css"),
      "utf8",
    );
    expect(css).toContain("var(--ds-data-6");
  });
});
