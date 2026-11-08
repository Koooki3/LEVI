import { describe, expect, test } from "bun:test";
import { readFileSync } from "fs";
import { join } from "path";

const read = (...parts: string[]) =>
  readFileSync(join(import.meta.dir, "..", "..", ...parts), "utf8");
const code = (text: string) => text.replace(/\/\*[\s\S]*?\*\//g, "");

// `replay-panel.css` is imported by the panel, which the episode viewer loads
// lazily (and only for a dataset that has a replay). The layout class is
// already on the page before that chunk arrives, so a rule for it in that
// file makes the page jump when the chunk lands.
describe("the Episodes tab's replay layout", () => {
  test("`.vw-replay-layout` is styled in viewer.css, which the episode viewer imports up front", () => {
    const viewer = code(read("viewer", "viewer.css"));
    expect(viewer).toMatch(/\.vw-replay-layout\s*\{[^}]*display:\s*grid/);
    expect(viewer).toMatch(
      /@media \(min-width: 1200px\)\s*\{\s*\.vw-replay-layout\s*\{[^}]*repeat\(2/,
    );
    const page = read(
      "..",
      "app",
      "[org]",
      "[dataset]",
      "[episode]",
      "episode-viewer.tsx",
    );
    expect(page).toContain('import "@/components/viewer/viewer.css";');
    expect(page).toContain('className="vw-replay-layout"');
  });

  test("replay-panel.css, which loads with the lazy chunk, does not style it", () => {
    expect(code(read("robot-3d", "replay-panel.css"))).not.toContain(
      "vw-replay-layout",
    );
  });

  test("the panel is imported lazily by the episode viewer", () => {
    const page = read(
      "..",
      "app",
      "[org]",
      "[dataset]",
      "[episode]",
      "episode-viewer.tsx",
    );
    expect(page).toMatch(
      /const ReplayPanel = lazy\(\(\) => import\("@\/components\/robot-3d\/replay-panel"\)\)/,
    );
  });
});
