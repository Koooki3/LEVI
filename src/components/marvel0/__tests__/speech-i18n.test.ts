import { describe, expect, test } from "bun:test";
import { readFileSync } from "fs";
import { join } from "path";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";

// Every string of the voice card, in both languages (catalog.test.ts keeps the
// two catalogues equal as wholes and finds a t("…") without a key; the keys the
// card builds from tables are checked here).
const here = join(import.meta.dir, "..");
const SOURCES = [
  "speech-panel.tsx",
  "speech-session.ts",
  "speech-logic.ts",
  "speech-api.ts",
  "voice-capture.ts",
];
const PREFIX = /^marvel0\.(err\.)?speech\./;
const keysIn = (catalogue: Record<string, string>) =>
  Object.keys(catalogue).filter((key) => PREFIX.test(key));
const holes = (text: string) =>
  [...text.matchAll(/\{(\w+)\}/g)].map((match) => match[1]).sort();
const bare = (text: string) => text.replace(/\{\w+\}/g, "");

describe("the voice card's strings", () => {
  const enKeys = keysIn(en);

  test("exist in both languages, with the same placeholders, and Chinese is not the English", () => {
    expect(enKeys.length).toBeGreaterThan(90);
    expect(keysIn(zh).sort()).toEqual([...enKeys].sort());
    for (const key of enKeys) {
      const english = en[key as keyof typeof en];
      const chinese = zh[key as keyof typeof zh];
      expect(english, key).not.toBe("");
      expect(chinese, key).not.toBe(english);
      expect(holes(chinese), key).toEqual(holes(english));
    }
  });

  test("every key the code names is in the catalogue and every key of the catalogue is used", () => {
    const used = new Set<string>();
    for (const file of SOURCES) {
      const text = readFileSync(join(here, file), "utf8");
      for (const match of text.matchAll(
        /"(marvel0\.(?:err\.)?speech\.[A-Za-z0-9_.]+)"/g,
      ))
        used.add(match[1]);
    }
    expect([...used].filter((key) => !(key in en) || !(key in zh))).toEqual([]);
    expect(enKeys.filter((key) => !used.has(key))).toEqual([]);
  });

  test("a Chinese title, button or state name has no Latin letter (names go in the body text)", () => {
    const headline =
      /\.(title|edited)$|\.btn\.|\.engine\.|\.lang\.|\.problem\.|heard_as|^marvel0\.speech\.mic_on$/;
    const checked = enKeys.filter((key) => headline.test(key));
    expect(checked.length).toBeGreaterThan(25);
    for (const key of checked)
      expect(bare(zh[key as keyof typeof zh]), key).not.toMatch(/[A-Za-z]/);
  });

  test("Chinese puts a space between Chinese and Latin words or numbers", () => {
    for (const key of keysIn(zh)) {
      const text = zh[key as keyof typeof zh];
      expect(text, key).not.toMatch(/[一-鿿][A-Za-z0-9]/);
      expect(text, key).not.toMatch(/[A-Za-z0-9][一-鿿]/);
    }
  });

  test("no sentence says the page listens, or hears commands", () => {
    for (const key of enKeys)
      expect(en[key as keyof typeof en], key).not.toMatch(
        /listen|command word|voice command/i,
      );
  });

  test("the card tells the access address requirement in both languages", () => {
    for (const key of [
      "marvel0.speech.mic.insecure",
      "marvel0.speech.mic.denied",
    ] as const)
      for (const text of [en[key], zh[key]]) {
        expect(text).toContain("http://127.0.0.1:7860");
        expect(text).toContain("http://localhost:7860");
      }
  });

  test("states that Use fills only the task instruction and that the person still makes and copies the command", () => {
    expect(en["marvel0.speech.read_first"]).toContain(
      "does not make or run the command",
    );
    expect(en["marvel0.speech.used"]).toContain("copy the command yourself");
    expect(zh["marvel0.speech.used"]).toContain("复制命令");
  });
});
