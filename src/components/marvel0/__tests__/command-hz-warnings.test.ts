import { describe, expect, test } from "bun:test";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import { readPreview, warningSay, type PreviewWarning } from "../command-logic";
import { sayText } from "../marvel0-say";
import { RATE_CASES, preview } from "./command-fixtures";

const catalogue = (lang: "en" | "zh") => (key: string) =>
  (lang === "en" ? en : zh)[key as keyof typeof en] ?? key;

// The warnings exactly as the backend writes them at 50 Hz (levi/marvel0/command.py).
const warnings: PreviewWarning[] = [
  {
    code: "rtc_never_fits",
    text: "English",
    params: { hz: 50, beats: 15, chunk: 10, chunk_ms: 200 },
  },
  {
    code: "control_hz_not_trained",
    text: "English",
    params: { hz: 50, speed: "5" },
  },
  { code: "control_hz_untested", text: "English", params: { hz: 50 } },
  {
    code: "step_cap_tight",
    text: "English",
    params: {
      hz: 50,
      cap_m: 0.02,
      cap_rad: 0.07,
      p99_m: 0.0384,
      p99_rad: 0.0883,
    },
  },
  {
    code: "episode_short",
    text: "English",
    params: { steps: 500, hz: 50, seconds: 10, minimum: 30 },
  },
  {
    code: "rtc_margin_low",
    text: "English",
    params: {
      horizon: 6,
      beats: 6,
      reached: 12,
      chunk: 10,
      hz: 20,
      beat_ms: 50,
    },
  },
];

describe("the warnings of a control rate, in the page's words", () => {
  test("each is worded in both languages with its numbers filled in", () => {
    for (const warning of warnings) {
      const worded = warningSay(warning);
      expect(worded.say, warning.code).not.toBeNull();
      for (const lang of ["en", "zh"] as const) {
        const text = sayText(worded.say!, catalogue(lang));
        expect([warning.code, lang, text]).toEqual([
          warning.code,
          lang,
          expect.not.stringMatching(/\{\w+\}|^marvel0\./),
        ]);
      }
      expect(sayText(worded.say!, catalogue("zh"))).toMatch(/[一-鿿]/);
    }
  });

  test("the numbers of the sentence are the backend's", () => {
    const say = (code: string, lang: "en" | "zh" = "en") =>
      sayText(
        warningSay(warnings.find((w) => w.code === code)!).say!,
        catalogue(lang),
      );
    expect(say("step_cap_tight")).toContain("0.02 m and 0.07 rad");
    expect(say("step_cap_tight")).toContain("0.0384 m or 0.0883 rad");
    expect(say("episode_short")).toContain(
      "500 steps at 50 Hz last at least 10 s",
    );
    expect(say("episode_short", "zh")).toContain(
      "50 Hz 下，500 步至少持续 10 秒",
    );
    expect(say("control_hz_not_trained")).toContain("5 times that speed");
    expect(say("rtc_never_fits")).toContain("200 ms");
    expect(say("rtc_margin_low")).toContain("reaches 12 of 10 actions");
    expect(say("rtc_margin_low")).toContain("a beat is 50 ms");
  });

  test("the first-use note names the owner and the physical e-stop in both languages", () => {
    for (const lang of ["en", "zh"] as const) {
      const text = sayText(
        warningSay(warnings.find((w) => w.code === "control_hz_untested")!)
          .say!,
        catalogue(lang),
      );
      expect(text).toMatch(
        lang === "en" ? /owner present.*e-stop/ : /机主在场.*急停/,
      );
    }
  });

  test("the RTC margin at 10 Hz (no numbers sent) is the sentence the page always had", () => {
    expect(warningSay({ code: "rtc_margin_low", text: "English" }).say).toEqual(
      { key: "marvel0.cmd.warning.rtc_margin_low" },
    );
  });

  test("a rate warning without its numbers is shown as the server wrote it, never with a bare placeholder", () => {
    for (const code of [
      "control_hz_not_trained",
      "control_hz_untested",
      "step_cap_tight",
      "episode_short",
      "rtc_never_fits",
    ])
      expect(warningSay({ code, text: "As written" })).toEqual({
        say: null,
        text: "As written",
      });
  });

  test("the preview keeps the numbers of a warning, and only plain ones", () => {
    const view = readPreview(
      preview({
        warnings: [
          {
            code: "episode_short",
            text: "English",
            params: {
              steps: 500,
              hz: 50,
              bad: { a: 1 },
              worse: [1],
              none: null,
            },
          },
          { code: "min_z_below_task_table", text: "English" },
        ],
      }),
    )!;
    expect(view.warnings[0]).toEqual({
      code: "episode_short",
      text: "English",
      params: { steps: 500, hz: 50 },
    });
    expect(view.warnings[1]).toEqual({
      code: "min_z_below_task_table",
      text: "English",
    });
  });

  test("every warning code the backend sends for the sample rates has a sentence of the page", () => {
    const known = new Set([
      "unverified_model",
      "rtc_margin_low",
      "min_z_below_task_table",
      "control_hz_not_trained",
      "control_hz_untested",
      "step_cap_tight",
      "episode_short",
      "rtc_never_fits",
    ]);
    const sent = new Set(RATE_CASES.flatMap((c) => c.warnings));
    expect(sent.size).toBeGreaterThanOrEqual(6);
    for (const code of sent)
      expect([code, known.has(code)]).toEqual([code, true]);
  });
});
