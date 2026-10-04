import { describe, expect, test } from "bun:test";
import { ESLint } from "eslint";
import { join } from "path";

const root = join(import.meta.dir, "../..");

/** Messages of the frame's hex-colour rule for `code` in a frame file. */
async function hexHits(
  code: string,
  file = "src/components/shell/probe.tsx",
): Promise<number> {
  const eslint = new ESLint({ cwd: root });
  const [result] = await eslint.lintText(code, {
    filePath: join(root, file),
  });
  return result.messages.filter((m) => m.ruleId === "no-restricted-syntax")
    .length;
}

describe("ESLint: no hex colours in the global frame", () => {
  test("flags colours in strings, styles and templates", async () => {
    expect(await hexHits('export const a = "#fff";\n')).toBe(1);
    expect(await hexHits('export const a = "#ffffff80";\n')).toBe(1);
    expect(await hexHits("export const b = `color: #12ab34;`;\n")).toBe(1);
    expect(
      await hexHits('export const c = <div style={{ color: "#abcdef" }} />;\n'),
    ).toBe(1);
    expect(await hexHits('export const d = "border: 1px solid #abc";\n')).toBe(
      1,
    );
  }, 30000);

  test("leaves links, ids and other #words alone", async () => {
    expect(await hexHits('export const a = <a href="#add">x</a>;\n')).toBe(0);
    expect(await hexHits('export const b = { href: "#face" };\n')).toBe(0);
    expect(await hexHits('export const c = <div id="#bad" />;\n')).toBe(0);
    expect(await hexHits('export const d = "#heading";\n')).toBe(0);
    expect(await hexHits('export const e = "#12345";\n')).toBe(0);
    expect(await hexHits('export const f = "/guide#add-dataset";\n')).toBe(0);
  }, 30000);

  test("covers the home page, guide, report and token hook", async () => {
    for (const file of [
      "src/app/page.tsx",
      "src/app/guide/page.tsx",
      "src/components/home/probe.tsx",
      "src/components/report/probe.tsx",
      "src/lib/design/probe.ts",
    ])
      expect(await hexHits('export const a = "#fff";\n', file)).toBe(1);
  }, 30000);
});
