import { describe, expect, test } from "bun:test";
import { readFileSync, readdirSync } from "fs";
import { join } from "path";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";

const dirs = [
  join(import.meta.dir, ".."),
  join(import.meta.dir, "../../../app/automatic"),
];
function sources(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) =>
    entry.isDirectory()
      ? entry.name === "__tests__"
        ? []
        : sources(join(dir, entry.name))
      : /\.tsx?$/.test(entry.name)
        ? [join(dir, entry.name)]
        : [],
  );
}

describe("the run pages' catalogue keys", () => {
  const used = new Set<string>();
  for (const file of dirs.flatMap(sources))
    for (const match of readFileSync(file, "utf8").matchAll(
      /"(automatic\.run\.[A-Za-z0-9_.]+)"/g,
    ))
      used.add(match[1]);

  test("every key the code names exists in both languages", () => {
    expect(used.size).toBeGreaterThan(100);
    const missing = [...used].filter((k) => !(k in en && k in zh));
    expect(missing).toEqual([]);
  });
  test("no automatic.run key is left in the catalogues unused", () => {
    const all = Object.keys(en).filter((k) => k.startsWith("automatic.run."));
    expect(all.filter((k) => !used.has(k))).toEqual([]);
    expect(
      Object.keys(zh)
        .filter((k) => k.startsWith("automatic.run."))
        .sort(),
    ).toEqual(all.sort());
  });
  test("placeholders stay in both languages", () => {
    for (const key of Object.keys(en).filter((k) =>
      k.startsWith("automatic.run."),
    )) {
      const a = (en as Record<string, string>)[key].match(/\{\w+\}/g) ?? [];
      const b = (zh as Record<string, string>)[key].match(/\{\w+\}/g) ?? [];
      expect(b.sort()).toEqual(a.sort());
    }
  });
});
