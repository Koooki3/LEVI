import { describe, expect, test } from "bun:test";
import { ESLint } from "eslint";
import { join } from "path";

const root = join(import.meta.dir, "../../../..");

async function hexHits(code: string, file: string): Promise<number> {
  const eslint = new ESLint({ cwd: root });
  const [result] = await eslint.lintText(code, { filePath: join(root, file) });
  return result.messages.filter((m) => m.ruleId === "no-restricted-syntax")
    .length;
}

describe("ESLint: no hex colours in the episode viewer", () => {
  test("flags a colour in the viewer's files", async () => {
    const code = 'export const a = "#ffffff";\n';
    expect(await hexHits(code, "src/components/viewer/probe.tsx")).toBe(1);
    expect(await hexHits(code, "src/components/side-nav.tsx")).toBe(1);
    expect(
      await hexHits(code, "src/app/[org]/[dataset]/[episode]/probe.tsx"),
    ).toBe(1);
  }, 30000);

  test("the palette module is the one place for media colours", async () => {
    const code = 'export const a = "#ffffff";\n';
    expect(await hexHits(code, "src/components/viewer/data-palette.ts")).toBe(
      0,
    );
  }, 30000);
});
