import { describe, expect, test } from "bun:test";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import { errorOf, type ApiFailure } from "../deploy-api";
import { readSite } from "../catalog-logic";
import { sayText } from "../marvel0-say";
import {
  failureDetail,
  fieldMessages,
  sayFailure,
  sayStopRefusal,
} from "../marvel0-failure";
import { FORM_CODES, GUARD_CODES, REFUSALS } from "./backend-codes";
import { siteAnswer } from "./catalog-fixtures";

const fail = (
  code: string,
  extra: Record<string, unknown> = {},
  status = 409,
  message = "The server's English sentence",
): ApiFailure => errorOf(status, { detail: { code, message, ...extra } });

const catalogue = (lang: "en" | "zh") => (key: string) =>
  (lang === "en" ? en : zh)[key as keyof typeof en] ?? key;
const words = (failure: ApiFailure, lang: "en" | "zh", ctx = {}) => {
  const worded = sayFailure(failure, ctx);
  return worded.say ? sayText(worded.say, catalogue(lang)) : worded.text;
};

describe("sayFailure", () => {
  test("every code the deploy backend refuses with has words in both languages", () => {
    expect(REFUSALS.length).toBeGreaterThan(30);
    for (const code of REFUSALS) {
      const failure = fail(code, {
        phrase: "deploy m",
        port: 8000,
        listeners: 1,
        ours: false,
        established: 2,
        units: ["levi-aeri-policy-manual-marvel0-m"],
        run_id: "r1",
        fraction: 0.3,
        total_mib: 32607,
        free_mib: 5100,
        required_mib: 10807,
        reserved_mib: 9783,
        margin_mib: 1024,
        unit: "levi-aeri-policy-manual-marvel0-m",
        exit_code: 1,
        file: "scripts/serve_policy_rtc.py",
        operation: { action: "deploy", request_id: "x", started_at: "t" },
      });
      const worded = sayFailure(failure);
      expect(worded.say, code).not.toBeNull();
      for (const lang of ["en", "zh"] as const) {
        const text = words(failure, lang);
        // The catalogue was found: a missing key would print the key itself.
        expect(text, `${code} ${lang}`).not.toMatch(/^marvel0\./);
        expect(text, `${code} ${lang}`).not.toContain("{");
        expect(text, `${code} ${lang}`).not.toContain(failure.message);
      }
      expect(words(failure, "zh"), code).toMatch(/[一-鿿]/);
    }
  });

  test("every code of the command form and of the guard has words in both languages", () => {
    for (const code of [...FORM_CODES, ...GUARD_CODES]) {
      const failure = fail(code, {
        field: "prompt",
        reason: "empty",
        min: 1,
        max: 3000,
        floor: 0.025,
        home: 0.235951,
        looked_at: "/path/to/site.toml",
        errors: [{ field: "paths.python", code: "missing" }],
      });
      for (const lang of ["en", "zh"] as const) {
        const text = words(failure, lang, { site: readSite(siteAnswer()) });
        expect(text, `${code} ${lang}`).not.toMatch(/^marvel0\./);
        expect(text, `${code} ${lang}`).not.toContain(failure.message);
      }
      expect(words(failure, "zh"), code).toMatch(/[一-鿿]/);
    }
  });

  test("a code the page does not know is shown as the server wrote it", () => {
    const failure = fail("brand_new_refusal");
    expect(sayFailure(failure)).toEqual({
      say: null,
      text: "The server's English sentence",
    });
  });

  test("no answer is worded for a read and for a write, and the write says it is not known", () => {
    const failure = errorOf(0, null);
    const network: ApiFailure = {
      ...failure,
      code: "network",
      status: 0,
      message: "fetch failed",
    };
    expect(words(network, "en")).toContain("did not answer");
    const write = words(network, "en", { write: true });
    expect(write).toContain("not known whether");
    expect(write).toContain("never carried out twice");
  });

  test("the numbers of a memory refusal are in the sentence", () => {
    const text = words(
      fail("gpu_insufficient", {
        fraction: 0.3,
        total_mib: 32607,
        free_mib: 5100,
        required_mib: 10807,
        reserved_mib: 9783,
        margin_mib: 1024,
      }),
      "en",
    );
    expect(text).toContain("0.30");
    for (const n of ["32607", "10807", "9783", "1024", "5100"])
      expect(text).toContain(n);
    const chinese = words(
      fail("gpu_insufficient", {
        fraction: 0.3,
        total_mib: 32607,
        free_mib: 5100,
        required_mib: 10807,
        reserved_mib: 9783,
        margin_mib: 1024,
      }),
      "zh",
    );
    for (const n of ["0.30", "32607", "10807", "9783", "1024", "5100"])
      expect(chinese).toContain(n);
  });

  test("a busy refusal names the operation in the page's words", () => {
    const busy = fail(
      "busy",
      {
        operation: { action: "stop", request_id: "abcdefgh", started_at: "t" },
      },
      423,
    );
    expect(words(busy, "en")).toContain("stop");
    expect(words(fail("busy", { operation: null }, 423), "en")).toContain(
      "operation",
    );
  });

  test("the missing site file names where it looked and nothing else", () => {
    const text = words(
      fail(
        "site_missing",
        { looked_at: "/x/lab/marvel0/site.toml", template: "# t" },
        412,
      ),
      "en",
    );
    expect(text).toContain("/x/lab/marvel0/site.toml");
    expect(text).not.toContain("# t");
  });

  test("a refusal that is only a status line says the status", () => {
    const text = words(errorOf(503, "<html>"), "en");
    expect(text).toContain("503");
    expect(words(errorOf(503, "<html>"), "zh")).toContain("503");
  });

  test("a stop that needs the person to finish it names the unit to stop by hand", () => {
    const text = words(
      fail(
        "stop_incomplete",
        { unit: "levi-aeri-policy-manual-marvel0-m" },
        502,
      ),
      "en",
    );
    expect(text).toContain(
      "systemctl --user stop levi-aeri-policy-manual-marvel0-m",
    );
  });

  test("prompt and note refusals say which rule, with the site's limit", () => {
    const ctx = { site: readSite(siteAnswer()) };
    const reasons: Record<string, string> = {
      empty: "Enter",
      too_long: "300",
      forbidden_char: "quote",
      leading_dash: "dash",
      control_char: "control",
    };
    for (const [reason, word] of Object.entries(reasons)) {
      expect(
        words(
          fail("prompt_invalid", { reason, field: "prompt" }, 422),
          "en",
          ctx,
        ),
      ).toContain(word);
    }
    expect(
      words(
        fail("note_invalid", { reason: "too_long", field: "note" }, 422),
        "en",
        ctx,
      ),
    ).toContain("200");
    // A reason the page does not know still gives the generic sentence.
    expect(
      words(
        fail("prompt_invalid", { reason: "new", field: "prompt" }, 422),
        "en",
        ctx,
      ),
    ).toContain("cannot be used");
  });

  test("a task instruction or note refusal lists the characters it cannot contain", () => {
    for (const code of ["prompt_invalid", "note_invalid"]) {
      const text = words(
        fail(code, { reason: "forbidden_char", field: "prompt" }, 422),
        "en",
        { site: readSite(siteAnswer()) },
      );
      for (const char of ["'", '"', "\\", "$", "`", "!"])
        expect(text, `${code} ${char}`).toContain(char);
      expect(
        words(fail(code, { reason: "forbidden_char" }, 422), "zh"),
      ).toContain("$");
    }
  });

  test("range refusals carry their bounds", () => {
    expect(
      words(
        fail(
          "max_steps_out_of_range",
          { min: 1, max: 3000, field: "max_steps" },
          422,
        ),
        "en",
      ),
    ).toMatch(/1.*3000/);
    expect(
      words(
        fail("min_z_below_floor", { floor: 0.025, field: "min_z" }, 422),
        "en",
      ),
    ).toContain("0.025");
    expect(
      words(
        fail("min_z_above_home", { home: 0.235951, field: "min_z" }, 422),
        "en",
      ),
    ).toContain("0.235951");
  });
});

describe("the stop's own refusals", () => {
  test("a refused stop with a client connected points at the override phrase", () => {
    const say = sayStopRefusal(fail("client_connected", { established: 1 }));
    expect(sayText(say.say!, catalogue("en"))).toContain("type");
  });
});

describe("failureDetail", () => {
  test("only the facts that are the server's own words are passed on", () => {
    expect(
      failureDetail(
        fail(
          "start_failed",
          { exit_code: 1, detail: "Failed to set property X" },
          502,
        ),
      ),
    ).toBe("Failed to set property X");
    expect(failureDetail(fail("port_taken", { port: 8000 }))).toBeNull();
    expect(
      failureDetail(fail("start_failed", { exit_code: 1 }, 502)),
    ).toBeNull();
  });
});

describe("fieldMessages", () => {
  test("a form refusal belongs to its field", () => {
    const failure = fail(
      "max_steps_out_of_range",
      { min: 1, max: 3000, field: "max_steps" },
      422,
    );
    const messages = fieldMessages(failure, {});
    expect(Object.keys(messages)).toEqual(["max_steps"]);
    expect(sayText(messages.max_steps.say!, catalogue("en"))).toContain("3000");
  });

  test("FastAPI's per-field errors each belong to their field, in the page's words", () => {
    const failure = errorOf(422, {
      detail: [
        { loc: ["body", "min_z"], msg: "Input should be a valid number" },
        { loc: ["body", "note"], msg: "Input should be a valid string" },
      ],
    });
    const messages = fieldMessages(failure, {});
    expect(Object.keys(messages).sort()).toEqual(["min_z", "note"]);
    expect(messages.min_z.say).not.toBeNull();
  });

  test("a refusal about no field is not put on one", () => {
    expect(fieldMessages(fail("site_missing", {}, 412), {})).toEqual({});
    expect(fieldMessages(fail("command_self_check", {}, 500), {})).toEqual({});
  });
});
