import { describe, expect, test } from "bun:test";
import { readFileSync } from "fs";
import { join } from "path";
import { currentHeadingIndex, readingLine } from "../active-section";

const src = join(import.meta.dir, "../../..");
const css = (file: string) => readFileSync(join(src, file), "utf8");

describe("which section the contents mark", () => {
  test("a section is current once its heading is above the reading line", () => {
    const line = readingLine(900, 56);
    expect(line).toBe(360);
    // Headings at 700 (below), 200, -300 (above): the one at 200 is current,
    // not the one before it. A line at the top bar's edge would pick -300
    // until the 200 one reached the bar.
    expect(currentHeadingIndex([700, 200, -300], line)).toBe(2);
    expect(currentHeadingIndex([-400, 250, 700], line)).toBe(1);
    expect(currentHeadingIndex([500, 800], line)).toBe(-1);
    expect(currentHeadingIndex([null, 100, null], line)).toBe(1);
  });

  test("on a short window the line stays clear of the bar", () => {
    expect(readingLine(400, 56)).toBe(176);
    expect(readingLine(400, 0)).toBe(160);
  });
});

describe("reading styles", () => {
  test("no slanted CJK: Chinese emphasis is a weight", () => {
    expect(css("styles/reading.css")).toMatch(
      /:lang\(zh\) \.levi-prose :is\(em[^)]*\) \{[^}]*font-style:\s*normal/,
    );
  });

  test("the first column of a Markdown table keeps a readable width", () => {
    expect(css("app/report/report.css")).toMatch(
      /\.lr-md-table td:first-child \{[^}]*min-width:\s*6\.5em/,
    );
  });
});
