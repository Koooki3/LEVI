import { describe, expect, test } from "bun:test";
import { readModels, readSite } from "../catalog-logic";
import {
  commandBody,
  initialCommandForm,
  placeholders,
  rateFacts,
  type CommandForm,
} from "../command-logic";
import {
  controlFacts,
  latencyBeats,
  readControl,
  rtcHorizonDefault,
  scaledCap,
} from "../control-logic";
import { modelsAnswer } from "./catalog-fixtures";
import { RATE_CASES, RATE_CONTROL, siteWithRate } from "./command-fixtures";

const control = readControl(RATE_CONTROL)!;
const models = readModels(modelsAnswer())!;
const site = readSite(siteWithRate())!;

describe("the control block of the site answer", () => {
  test("is read as the backend writes it", () => {
    expect(control).toEqual({
      options: [5, 10, 20, 25, 50],
      default: 10,
      referenceHz: 10,
      actionHorizon: 10,
      latencyBeats: 3,
      shortEpisodeS: 30,
      stepCaps: { m: 0.1, rad: 0.35 },
      trainStepP99: { m: 0.0384, rad: 0.0883 },
    });
    expect(site.control).toEqual(control);
  });

  test("a site answer without it has no control (the form then has no rate field)", () => {
    expect(readSite(siteWithRate({ control: undefined }))!.control).toBeNull();
    expect(readControl(undefined)).toBeNull();
    expect(readControl("10 Hz")).toBeNull();
    expect(readControl([])).toBeNull();
  });

  test("a missing, zero, negative or non-number part is no control: a made-up limit would be worse than none", () => {
    const part = (path: string[], value: unknown) => {
      const copy = structuredClone(RATE_CONTROL) as Record<string, unknown>;
      let at: Record<string, unknown> = copy;
      for (const key of path.slice(0, -1))
        at = at[key] as Record<string, unknown>;
      if (value === undefined) delete at[path[path.length - 1]];
      else at[path[path.length - 1]] = value;
      return readControl(copy);
    };
    const paths = [
      ["options"],
      ["default"],
      ["reference_hz"],
      ["action_horizon"],
      ["latency_beats"],
      ["short_episode_s"],
      ["step_caps", "m"],
      ["step_caps", "rad"],
      ["train_step_p99", "m"],
      ["train_step_p99", "rad"],
    ];
    for (const path of paths)
      for (const bad of [undefined, 0, -1, "10", null, NaN])
        expect([path, part(path, bad)]).toEqual([path, null]);
    expect(part(["options"], [5, "10", 20])).toBeNull();
    expect(part(["options"], [])).toBeNull();
    // the default must be one of the options
    expect(part(["default"], 12)).toBeNull();
  });
});

describe("what a rate changes, worked out by the page", () => {
  test("the per-step caps: kept up to 10 Hz, divided by the rate ratio above it", () => {
    const at = (hz: number) => [
      scaledCap(control, 0.1, hz),
      scaledCap(control, 0.35, hz),
    ];
    expect(at(5)).toEqual([0.1, 0.35]);
    expect(at(10)).toEqual([0.1, 0.35]);
    expect(at(20)).toEqual([0.05, 0.175]);
    expect(at(25)).toEqual([0.04, 0.14]);
    expect(at(50)).toEqual([0.02, 0.07]);
    // the site's value is the one scaled, not a constant
    expect(scaledCap(control, 0.06, 25)).toBe(0.024);
    expect(scaledCap(control, 0.06, 5)).toBe(0.06);
  });

  test("the latency in beats of the rate, rounded up", () => {
    expect(control.options.map((hz) => latencyBeats(control, hz))).toEqual([
      2, 3, 6, 8, 15,
    ]);
  });

  test("an empty RTC horizon: the site's default up to 10 Hz, lowered above it, never below 1", () => {
    const at = (def: number, hz: number) => rtcHorizonDefault(control, def, hz);
    expect(control.options.map((hz) => at(6, hz))).toEqual([6, 6, 4, 2, 1]);
    expect(at(8, 5)).toBe(8);
    expect(at(8, 10)).toBe(8);
    expect(at(3, 20)).toBe(3);
  });

  test("the facts of a default form at each rate", () => {
    const facts = (hz: number) =>
      controlFacts(control, {
        hz,
        steps: 500,
        horizon: rtcHorizonDefault(control, 6, hz),
        rtc: true,
      });
    expect(facts(10)).toMatchObject({
      changed: false,
      periodMs: 100,
      episodeS: 50,
      speedCapMS: 1,
      speedCapRadS: 3.5,
      rtc: { horizon: 6, reached: 9, margin: 1 },
    });
    expect(facts(20)).toMatchObject({
      changed: true,
      stepCapM: 0.05,
      stepCapRad: 0.175,
      episodeS: 25,
      chunkS: 0.5,
      horizonS: 0.2,
      latencyBeats: 6,
      rtc: { horizon: 4, reached: 10, margin: 0 },
    });
    expect(facts(50).rtc).toEqual({ horizon: 1, reached: 16, margin: -6 });
    expect(facts(5)).toMatchObject({ speedCapMS: 0.5, speedCapRadS: 1.75 });
  });

  test("with RTC off there is no RTC margin", () => {
    expect(
      controlFacts(control, { hz: 20, steps: 500, horizon: 6, rtc: false }).rtc,
    ).toBeNull();
  });
});

describe("the page and the backend work out the same numbers", () => {
  test("the sample covers every option with RTC on, and the edges", () => {
    const rtcRates = RATE_CASES.filter((c) => c.effective.rtc).map(
      (c) => c.effective.hz,
    );
    expect([...new Set(rtcRates)].sort((a, b) => a - b)).toEqual(
      control.options,
    );
    expect(RATE_CASES.length).toBeGreaterThanOrEqual(12);
  });

  for (const sample of RATE_CASES)
    test(`${sample.name}: every derived value, and the horizon of an empty field`, () => {
      const { hz, max_steps: steps, rtc, horizon } = sample.effective;
      const mine = controlFacts(control, { hz, steps, horizon, rtc });
      const theirs = sample.control;
      expect({
        hz: mine.hz,
        period_ms: mine.periodMs,
        episode_s: mine.episodeS,
        step_cap_m: mine.stepCapM,
        step_cap_rad: mine.stepCapRad,
        speed_cap_m_s: mine.speedCapMS,
        speed_cap_rad_s: mine.speedCapRadS,
        chunk_s: mine.chunkS,
        horizon_s: mine.horizonS,
        latency_beats: mine.latencyBeats,
        rtc: mine.rtc,
      }).toEqual({
        hz: theirs.hz,
        period_ms: theirs.period_ms,
        episode_s: theirs.episode_s,
        step_cap_m: theirs.step_cap_m,
        step_cap_rad: theirs.step_cap_rad,
        speed_cap_m_s: theirs.speed_cap_m_s,
        speed_cap_rad_s: theirs.speed_cap_rad_s,
        chunk_s: theirs.chunk_s,
        horizon_s: theirs.horizon_s,
        latency_beats: theirs.latency_beats,
        rtc: theirs.rtc,
      });
      // the flags the command carries are the ones the facts name
      expect(sample.flags).toContain(`--max-step-m=${mine.stepCapM}`);
      expect(sample.flags).toContain(`--max-step-rad=${mine.stepCapRad}`);
      expect(sample.flags.some((f) => f === `--control-hz=${hz}`)).toBe(
        hz !== 10,
      );
    });

  for (const sample of RATE_CASES.filter(
    (c) => !("rtc_horizon" in c.body) && c.effective.rtc,
  ))
    test(`${sample.name}: an empty horizon field shows the horizon the server uses`, () => {
      const form: CommandForm = {
        ...initialCommandForm(models, site),
        rtc: true,
        hz: String(sample.effective.hz),
      };
      expect(placeholders(form, models, site).horizon).toBe(
        String(sample.effective.horizon),
      );
      expect(rateFacts(form, models, site)!.horizon).toBe(
        sample.effective.horizon,
      );
    });
});

describe("the facts of the form as it is typed", () => {
  const form = (over: Partial<CommandForm>): CommandForm => ({
    ...initialCommandForm(models, site),
    ...over,
  });

  test("an empty or unreadable steps or horizon field counts as its default, a typed one as typed", () => {
    expect(rateFacts(form({ hz: "20" }), models, site)).toMatchObject({
      steps: 500,
      horizon: 4,
      episodeS: 25,
    });
    expect(
      rateFacts(
        form({ hz: "20", maxSteps: "1000", horizon: "5" }),
        models,
        site,
      ),
    ).toMatchObject({ steps: 1000, horizon: 5, episodeS: 50 });
    for (const bad of ["x", "0", "-5", "5.5", "  "])
      expect(
        rateFacts(
          form({ hz: "20", maxSteps: bad, horizon: bad }),
          models,
          site,
        ),
      ).toMatchObject({ steps: 500, horizon: 4 });
  });

  test("a site that offers no rate has no facts", () => {
    const plain = readSite(siteWithRate({ control: undefined }))!;
    expect(rateFacts(form({ hz: "20" }), models, plain)).toBeNull();
  });

  test("the horizon an RTC-off form shows is the site's open-loop default at every rate", () => {
    for (const hz of ["5", "20", "50"])
      expect(
        placeholders(form({ hz, rtc: false }), models, site),
      ).toMatchObject({ horizon: "6", horizonLoweredFrom: null });
  });

  test("the body names the rate only when it is not the default one", () => {
    expect(commandBody(form({ hz: "10" }), 10).body).not.toHaveProperty(
      "control_hz",
    );
    expect(commandBody(form({ hz: "20" }), 10).body.control_hz).toBe(20);
    expect(commandBody(form({ hz: "5" }), 10).body.control_hz).toBe(5);
    // the default is the server's: a site whose default were 20 omits 20
    expect(commandBody(form({ hz: "20" }), 20).body).not.toHaveProperty(
      "control_hz",
    );
    expect(commandBody(form({ hz: "10" }), 20).body.control_hz).toBe(10);
  });

  test("a rate that is not a whole number is an error on its field, with no request", () => {
    const { errors } = commandBody(form({ hz: "12.5" }), 10);
    expect(Object.keys(errors)).toEqual(["control_hz"]);
  });
});
