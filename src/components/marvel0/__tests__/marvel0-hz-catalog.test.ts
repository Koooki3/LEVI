import { describe, expect, test } from "bun:test";
import { readFileSync, readdirSync } from "fs";
import { join } from "path";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import { warningSay } from "../command-logic";
import { errorOf, type ApiFailure } from "../deploy-api";
import { fieldMessages, sayFailure } from "../marvel0-failure";
import { sayText } from "../marvel0-say";
import { FORM_CODES } from "./backend-codes";

const E = en as Record<string, string>;
const Z = zh as Record<string, string>;
const ours = Object.keys(E).filter((key) => key.startsWith("marvel0.hz."));
const root = join(import.meta.dir, "../../../..");
const catalogue = (lang: "en" | "zh") => (key: string) =>
  (lang === "en" ? E : Z)[key] ?? key;

describe("the control-rate sentences (marvel0.hz.*)", () => {
  test("every key is in both languages, and no Chinese text is empty or the English", () => {
    expect(ours.length).toBeGreaterThan(30);
    for (const key of ours) {
      expect(Z[key], key).toBeTruthy();
      if (E[key].length > 12) expect(Z[key], key).not.toBe(E[key]);
    }
    expect(Object.keys(Z).filter((k) => k.startsWith("marvel0.hz."))).toEqual(
      ours,
    );
  });

  test("a headline in Chinese has no Latin letter (names go in the text that follows)", () => {
    const headlines = ours.filter((key) => /(\.title|\.fact\.\w+)$/.test(key));
    expect(headlines.length).toBeGreaterThan(5);
    for (const key of headlines)
      expect([key, Z[key].replace(/\{\w+\}/g, "").match(/[A-Za-z]/g)]).toEqual([
        key,
        null,
      ]);
  });

  test("a space stands between a Chinese character and a Latin word", () => {
    for (const key of ours)
      expect([key, Z[key].match(/[一-鿿][A-Za-z]|[A-Za-z][一-鿿]/g)]).toEqual([
        key,
        null,
      ]);
  });

  test("the Chinese text keeps the placeholders of the English", () => {
    const names = (text: string) =>
      [...text.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort();
    for (const key of ours)
      expect([key, names(Z[key])]).toEqual([key, names(E[key])]);
  });

  test("every key is used by the page, and every key the page asks for exists", () => {
    const files: string[] = [];
    const walk = (dir: string) => {
      for (const entry of readdirSync(dir, { withFileTypes: true })) {
        if (entry.name === "__tests__") continue;
        const path = join(dir, entry.name);
        if (entry.isDirectory()) walk(path);
        else if (/\.(ts|tsx)$/.test(entry.name)) files.push(path);
      }
    };
    walk(join(root, "src/components/marvel0"));
    const code = files.map((file) => readFileSync(file, "utf8")).join("\n");
    const used = new Set(
      [...code.matchAll(/"(marvel0\.hz\.[a-z_.]+)"/g)].map((m) => m[1]),
    );
    expect([...used].filter((key) => !(key in E))).toEqual([]);
    expect(ours.filter((key) => !used.has(key))).toEqual([]);
  });

  test("no sentence calls anything an emergency stop: it only points at the physical one", () => {
    for (const key of ours)
      for (const text of [E[key], Z[key]])
        expect(text.replace(/physical e-stop|实体急停/g, "")).not.toMatch(
          /e-?stop|emergency|急停/i,
        );
  });
});

describe("the backend's words against the page's", () => {
  test("the refusal of an unknown rate is a form code, worded for its field in both languages", () => {
    expect(FORM_CODES).toContain("control_hz_unsupported");
    const failure: ApiFailure = errorOf(422, {
      detail: {
        code: "control_hz_unsupported",
        message: "English",
        field: "control_hz",
        options: [5, 10, 20, 25, 50],
      },
    });
    const messages = fieldMessages(failure, {});
    expect(Object.keys(messages)).toEqual(["control_hz"]);
    expect(sayText(messages.control_hz.say!, catalogue("en"))).toBe(
      "This control rate is not offered. Choose one of 5, 10, 20, 25, 50 Hz.",
    );
    expect(sayText(messages.control_hz.say!, catalogue("zh"))).toBe(
      "没有这个控制频率。请从 5, 10, 20, 25, 50 Hz 里选一个。",
    );
    // without the options the sentence still has no bare placeholder
    const bare = sayFailure(
      errorOf(422, {
        detail: {
          code: "control_hz_unsupported",
          message: "x",
          field: "control_hz",
        },
      }),
    );
    expect(sayText(bare.say!, catalogue("en"))).not.toContain("{");
  });

  test("every warning code the backend can send has a sentence of the page", () => {
    const python = readFileSync(join(root, "levi/marvel0/command.py"), "utf8");
    const codes = [
      ...new Set(
        [...python.matchAll(/_warning\(\s*"([a-z_]+)"/g)].map((m) => m[1]),
      ),
    ];
    expect(codes).toEqual(
      expect.arrayContaining([
        "unverified_model",
        "rtc_margin_low",
        "rtc_never_fits",
        "min_z_below_task_table",
        "control_hz_not_trained",
        "control_hz_untested",
        "step_cap_tight",
        "episode_short",
      ]),
    );
    for (const code of codes) {
      const worded = warningSay({ code, text: "English", params: { n: 1 } });
      expect([code, worded.say === null]).toEqual([code, false]);
    }
  });
});
