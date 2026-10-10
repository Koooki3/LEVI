// Every catalogue key the wizard, setup and campaign code names exists in both
// languages, and the two languages have the same FE3 keys. (The shared catalogue
// test checks t("…") literals; keys held in tables are checked here.)
import { describe, expect, test } from "bun:test";
import { readdirSync, readFileSync } from "fs";
import { join } from "path";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";

const PREFIX = /^automatic\.(wizard|campaign|setup)\./;
const dirs = [
  join(import.meta.dir, ".."),
  join(import.meta.dir, "../../../app/automatic"),
];

function sources(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name !== "__tests__") out.push(...sources(path));
    } else if (/\.(ts|tsx)$/.test(entry.name)) out.push(path);
  }
  return out;
}

describe("the FE3 language keys", () => {
  test("English and Chinese have the same keys under the FE3 prefixes", () => {
    const e = Object.keys(en).filter((k) => PREFIX.test(k));
    const z = Object.keys(zh).filter((k) => PREFIX.test(k));
    expect(e.filter((k) => !(k in zh))).toEqual([]);
    expect(z.filter((k) => !(k in en))).toEqual([]);
    expect(e.length).toBeGreaterThan(0);
  });

  test("every key the code names is in both catalogues", () => {
    const missing: string[] = [];
    for (const dir of dirs) {
      let files: string[] = [];
      try {
        files = sources(dir);
      } catch {
        continue;
      }
      for (const file of files) {
        const text = readFileSync(file, "utf8");
        for (const m of text.matchAll(
          /["'`](automatic\.(?:wizard|campaign|setup)\.[A-Za-z0-9_.]+)["'`]/g,
        )) {
          const key = m[1];
          if (!(key in en && key in zh)) missing.push(`${file} ${key}`);
        }
      }
    }
    expect(missing).toEqual([]);
  });
});
