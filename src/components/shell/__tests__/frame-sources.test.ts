import { describe, expect, test } from "bun:test";
import { readFileSync } from "fs";
import { join } from "path";

const src = join(import.meta.dir, "../../..");

describe("frame sources", () => {
  test("no window.confirm anywhere in the interface", () => {
    const hits = [...new Bun.Glob("**/*.{ts,tsx}").scanSync(src)].filter(
      (file) => {
        if (file.includes("__tests__")) return false;
        const code = readFileSync(join(src, file), "utf8")
          .replace(/\/\*[\s\S]*?\*\//g, "")
          .replace(/\/\/.*$/gm, "");
        return /\bwindow\.confirm\s*\(|(?<![.\w])confirm\s*\(\s*[`"']/.test(
          code,
        );
      },
    );
    expect(hits).toEqual([]);
  });
});
