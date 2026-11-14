import { describe, expect, test } from "bun:test";
import { readFileSync } from "fs";
import { join } from "path";
import { THEME_COLOR } from "../theme-color";

const tokens = readFileSync(
  join(import.meta.dir, "../../../styles/tokens.css"),
  "utf8",
);
const value = (name: string) =>
  new RegExp(`--ds-${name}:\\s*(#[0-9a-fA-F]{6})`)
    .exec(tokens)?.[1]
    .toLowerCase();

describe("the browser chrome colour", () => {
  test("is the page background of each theme (--ds-bg → gray step)", () => {
    // light: --ds-bg: var(--ds-gray-l-2); dark: var(--ds-gray-d-1)
    expect(THEME_COLOR.light).toBe(value("gray-l-2"));
    expect(THEME_COLOR.dark).toBe(value("gray-d-1"));
    expect(tokens).toMatch(/--ds-bg:\s*var\(--ds-gray-l-2\)/);
    expect(tokens).toMatch(/--ds-bg:\s*var\(--ds-gray-d-1\)/);
  });
});
