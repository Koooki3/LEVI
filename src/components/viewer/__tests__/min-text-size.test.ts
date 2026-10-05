import { describe, expect, test } from "bun:test";
import { readFileSync } from "fs";
import { join } from "path";

const components = join(import.meta.dir, "../..");

/** Files that draw text on a canvas: every size is at least 12 px. */
const CANVAS_FILES = [
  "live-segmentation-canvas.tsx",
  "video-overlay-canvas.tsx",
  "colormapped-video.tsx",
  "annotations-timeline.tsx",
];

describe("text drawn on canvas", () => {
  test.each(CANVAS_FILES)("%s uses 12 px or larger", (file) => {
    const text = readFileSync(join(components, file), "utf8");
    const sizes = [
      ...text.matchAll(/\.font\s*=\s*["'`][^"'`]*?(\d+(?:\.\d+)?)px/g),
    ].map((m) => Number(m[1]));
    expect(sizes.filter((size) => size < 12)).toEqual([]);
  });

  test("the live overlay label is drawn at 12 px", () => {
    const text = readFileSync(
      join(components, "live-segmentation-canvas.tsx"),
      "utf8",
    );
    expect(text).toContain('ctx.font = "12px ui-sans-serif, system-ui"');
  });
});
