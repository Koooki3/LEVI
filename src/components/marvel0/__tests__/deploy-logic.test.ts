import { describe, expect, test } from "bun:test";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import { readModels, readSite } from "../catalog-logic";
import {
  DEPLOY_STATES,
  STATE_KEY,
  STATE_TONE,
  checkRows,
  deployBlock,
  deployBody,
  deployDelay,
  deployPhrase,
  deployResult,
  initialForm,
  liveSay,
  newRequestId,
  noteSay,
  overridePhrase,
  parseScale,
  presetRows,
  readDeploy,
  readLog,
  resolveForm,
  readinessRows,
  shownDeploy,
  reasonSay,
  failureSay,
  stateSay,
  stopBody,
  stopControl,
  stopPhrase,
  stopResult,
  wantLog,
  type DeployForm,
  type DeployStatus,
} from "../deploy-logic";
import { sayText } from "../marvel0-say";
import type { Worded } from "../marvel0-json";
import type { Say } from "../marvel0-logic";
import { CHECKS, FAILURES, REASONS } from "./backend-codes";
import { modelsAnswer, siteAnswer } from "./catalog-fixtures";
import {
  UNIT,
  checks,
  deployInfo,
  deployed,
  idle,
  logAnswer,
  ready,
  starting,
  stopped,
} from "./deploy-fixtures";

const catalogue = (lang: "en" | "zh") => (key: string) =>
  (lang === "en" ? en : zh)[key as keyof typeof en] ?? key;
const say = (value: Say, lang: "en" | "zh" = "en") =>
  sayText(value, catalogue(lang));
const worded = (value: Worded<Say> | null, lang: "en" | "zh" = "en") =>
  value ? (value.say ? say(value.say, lang) : value.text) : null;

const models = readModels(modelsAnswer())!;
const site = readSite(siteAnswer())!;
const status = (data: Record<string, unknown> = idle()) =>
  readDeploy(data) as DeployStatus;
const sft = models.models[0];
const cfg = models.models[1];
const form = (over: Partial<DeployForm> = {}): DeployForm => ({
  modelId: sft.id,
  rtc: true,
  fraction: 0.22,
  scale: "",
  acceptUnverified: false,
  ...over,
});

describe("readDeploy", () => {
  test("reads an idle answer", () => {
    const s = status();
    expect(s.state).toBe("idle");
    expect(s.deploy).toBeNull();
    expect(s.unit).toEqual({ state: "absent", sub: null });
    expect(s.listener).toEqual({
      port: 8000,
      present: false,
      ours: null,
      established: 0,
    });
    expect(s.gpu).toMatchObject({ ok: true, totalMib: 32607, freeMib: 23507 });
    expect(s.canDeploy).toBe(true);
    expect(s.canStop).toBe(false);
    expect(s.checks).toHaveLength(9);
    expect(s.confirm).toEqual({
      deploy: "deploy {model_id}",
      stop: "stop policy server",
      clientOverride: "stop with client connected",
    });
    expect(s.notes[0].code).toBe("source_hash_unmanifested");
  });

  test("reads the record of a deploy", () => {
    const s = status(starting());
    expect(s.deploy).toMatchObject({
      unit: UNIT,
      modelId: "pi05_fr3_all_step49999",
      config: "pi05_fr3_all_state",
      rtc: true,
      fraction: 0.22,
      cfgScale: null,
      phase: "started",
      ageS: 41.2,
    });
    expect(s.canStop).toBe(true);
    expect(s.operation).toBeNull();
  });

  test("a state the page does not know is unknown, never idle", () => {
    expect(status(idle({ state: "paused" })).state).toBe("unknown");
    expect(status(idle({ state: 3 })).state).toBe("unknown");
    expect(status(idle({ state: undefined })).state).toBe("unknown");
  });

  test("a deploy or stop is offered only when the server says true", () => {
    expect(status(idle({ can_deploy: "true" })).canDeploy).toBe(false);
    expect(status(idle({ can_deploy: 1 })).canDeploy).toBe(false);
    expect(status(starting({ can_stop: "yes" })).canStop).toBe(false);
    expect(status(idle({ can_deploy: undefined })).canDeploy).toBe(false);
  });

  test("the hand-on-the-e-stop notice is on unless the server says it is not needed", () => {
    expect(status(idle()).estop.keepHand).toBe(false);
    expect(
      status(idle({ estop: { always: true, keep_hand: true } })).estop.keepHand,
    ).toBe(true);
    expect(status(idle({ estop: null })).estop.keepHand).toBe(true);
    expect(status(idle({ estop: { keep_hand: "no" } })).estop.keepHand).toBe(
      true,
    );
  });

  test("a wrong schema or a non-object is no answer", () => {
    expect(readDeploy(idle({ schema: "levi.marvel0.deploy.v2" }))).toBeNull();
    expect(readDeploy(null)).toBeNull();
    expect(readDeploy("x")).toBeNull();
    expect(readDeploy([])).toBeNull();
  });

  test("a part that is missing or of the wrong type is empty, not invented", () => {
    const s = status(
      idle({
        checks: "none",
        vram: null,
        gpu: null,
        listener: 4,
        confirm: {},
        stop_blockers: "x",
        notes: [1, { text: "no code" }],
      }),
    );
    expect(s.checks).toEqual([]);
    expect(s.vram.presets).toEqual([]);
    expect(s.gpu.ok).toBe(false);
    expect(s.gpu.freeMib).toBeNull();
    expect(s.listener.established).toBeNull();
    expect(s.confirm.deploy).toBe("deploy {model_id}");
    expect(s.stopBlockers).toEqual([]);
    expect(s.notes).toEqual([]);
  });

  test("a check with an unknown level is not taken for ok", () => {
    const s = status(idle({ checks: checks({ vram: { level: "fine" } }) }));
    expect(s.checks.find((c) => c.code === "vram")!.level).toBe("unknown");
  });
});

describe("the words of a state", () => {
  test("every state has a badge word and a tone; only ready is a success", () => {
    for (const state of DEPLOY_STATES) {
      expect(STATE_KEY[state]).toMatch(/^marvel0\.deploy\.state\./);
      expect(en).toHaveProperty([STATE_KEY[state]]);
      expect(zh).toHaveProperty([STATE_KEY[state]]);
    }
    expect(
      Object.entries(STATE_TONE).filter(([, t]) => t === "success"),
    ).toEqual([["ready", "success"]]);
    expect(STATE_TONE.idle).toBe("neutral");
    expect(STATE_TONE.unknown).toBe("neutral");
    expect(STATE_TONE.failed).toBe("danger");
  });

  test("each state has a sentence, with the port where it names one", () => {
    for (const state of DEPLOY_STATES) {
      const text = say(stateSay(status(idle({ state }))));
      expect(text).not.toMatch(/^marvel0\./);
      expect(text).not.toContain("{");
    }
    expect(say(stateSay(status(ready())))).toContain("8000");
    expect(
      say(stateSay(status(idle({ state: "foreign_listener" })))),
    ).toContain("8000");
  });

  test("every reason and failure the backend can give is worded in both languages", () => {
    expect(REASONS.length).toBeGreaterThan(5);
    expect(FAILURES.length).toBeGreaterThan(5);
    for (const code of REASONS) {
      const s = status(
        idle({ state: "foreign_listener", reason: { code, text: "English" } }),
      );
      const out = reasonSay(s)!;
      expect(out.say, code).not.toBeNull();
      for (const lang of ["en", "zh"] as const) {
        const text = worded(out, lang)!;
        expect(text, `${code} ${lang}`).not.toMatch(/^marvel0\./);
        expect(text).not.toContain("{");
      }
      expect(worded(out, "zh")).toMatch(/[一-鿿]/);
    }
    for (const kind of FAILURES) {
      const s = status(
        idle({ state: "failed", failure: { kind, text: "English" } }),
      );
      const out = failureSay(s)!;
      expect(out.say, kind).not.toBeNull();
      expect(worded(out, "zh")).toMatch(/[一-鿿]/);
    }
  });

  test("a reason the page does not know is shown as the server wrote it", () => {
    const s = status(idle({ reason: { code: "new_one", text: "Brand new" } }));
    expect(reasonSay(s)).toEqual({ say: null, text: "Brand new" });
    expect(reasonSay(status(idle()))).toBeNull();
    expect(failureSay(status(idle()))).toBeNull();
  });

  test("the port in a reason is the site's port", () => {
    const s = status(
      idle({
        state: "foreign_listener",
        reason: { code: "hand_started", text: "x" },
      }),
    );
    const text = worded(reasonSay(s))!;
    expect(text).toContain("8000");
    expect(text).toContain("Ctrl+C");
  });
});

describe("readiness", () => {
  test("lists what the log should have said for this deploy", () => {
    const rows = readinessRows(status(ready()));
    expect(rows.map((r) => [r.id, r.value])).toEqual([
      ["norm_stats", true],
      ["rtc_enabled", true],
      ["rtc_warmup", true],
      ["listening", true],
    ]);
  });

  test("a plain deploy has no RTC rows, a guidance deploy has the scale row", () => {
    const plain = status(starting({ deploy: deployInfo({ rtc: false }) }));
    expect(readinessRows(plain).map((r) => r.id)).toEqual([
      "norm_stats",
      "listening",
    ]);
    const guided = status(
      starting({
        deploy: deployInfo({ rtc: false, cfg_guidance_scale: 1 }),
        readiness: {
          norm_stats: true,
          rtc_enabled: null,
          rtc_warmup: null,
          cfg: false,
          listening: false,
        },
      }),
    );
    expect(readinessRows(guided).map((r) => [r.id, r.value])).toEqual([
      ["norm_stats", true],
      ["cfg", false],
      ["listening", false],
    ]);
  });

  test("nothing is listed without a deploy", () => {
    expect(readinessRows(status())).toEqual([]);
  });

  test("a part the log has not given yet is null, not no", () => {
    const rows = readinessRows(
      status(
        starting({
          readiness: {
            norm_stats: null,
            rtc_enabled: null,
            rtc_warmup: null,
            cfg: null,
            listening: null,
          },
        }),
      ),
    );
    expect(rows.every((r) => r.value === null)).toBe(true);
  });
});

describe("the deploy the state is about", () => {
  const last = deployInfo({ phase: "stopped" });
  const log = {
    norm_stats: true,
    rtc_enabled: true,
    rtc_warmup: true,
    cfg: null,
    listening: true,
  };

  test("is shown while it starts, runs, stops or has ended badly", () => {
    for (const state of ["starting", "ready", "failed", "dead", "stopping"])
      expect(
        shownDeploy(status(idle({ state, deploy: deployInfo() }))),
        state,
      ).not.toBeNull();
  });

  test("the record of a deploy that was stopped is nothing about the marvel0 now", () => {
    const s = status(idle({ deploy: last, readiness: log }));
    expect(s.deploy).not.toBeNull();
    expect(shownDeploy(s)).toBeNull();
    expect(readinessRows(s)).toEqual([]);
  });

  test("a server started by hand, or a state that cannot be read, shows no deploy of this page", () => {
    for (const state of ["foreign_listener", "unknown"])
      expect(shownDeploy(status(idle({ state, deploy: last })))).toBeNull();
  });
});

describe("memory per fraction", () => {
  test("shows the arithmetic of the server for each preset", () => {
    const rows = presetRows(status());
    expect(
      rows.map((r) => [r.label, r.requiredMib, r.reservedMib, r.enough]),
    ).toEqual([
      ["0.22", 8198, 7174, true],
      ["0.30", 10807, 9783, true],
      ["0.45", 15698, 14674, true],
    ]);
    expect(rows[0].freeMib).toBe(23507);
    expect(rows[0].marginMib).toBe(1024);
    expect(rows.map((r) => r.liveNote)).toEqual([false, true, true]);
    expect(say(rows[1].say)).toBe(
      "needs 10807 MiB free (9783 + 1024); 23507 MiB are free",
    );
  });

  test("a fraction that does not fit says how short the card is", () => {
    const small = idle({
      gpu: {
        state: "ok",
        total_mib: 32607,
        used_mib: 27507,
        free_mib: 5100,
        server_holds_mib: null,
        server_holds_basis: "reserved_fraction",
      },
      vram: {
        margin_mib: 1024,
        presets: [
          { fraction: 0.22, required_mib: 8198, enough: false },
          { fraction: 0.3, required_mib: 10807, enough: false },
        ],
        hint: "x",
      },
    });
    const rows = presetRows(status(small));
    expect(rows.map((r) => r.enough)).toEqual([false, false]);
    expect(say(rows[1].say)).toBe(
      "needs 10807 MiB free (9783 + 1024), but only 5100 MiB are free",
    );
  });

  test("an unreadable card gives no numbers and no yes", () => {
    const rows = presetRows(
      status(
        idle({
          gpu: {
            state: "unknown",
            total_mib: null,
            used_mib: null,
            free_mib: null,
          },
          vram: {
            margin_mib: 1024,
            presets: [{ fraction: 0.22, required_mib: null, enough: null }],
            hint: null,
          },
        }),
      ),
    );
    expect(rows[0].enough).toBeNull();
    expect(rows[0].requiredMib).toBeNull();
    expect(say(rows[0].say)).toBe("free memory cannot be read");
  });

  test("the card line and the memory a running server holds", () => {
    expect(liveSay(status())).toBeNull();
    expect(say(liveSay(status(starting()))!)).toBe(
      "The policy server holds 7174 MiB of GPU memory, so live annotation cannot start.",
    );
    const unknown = status(
      idle({
        state: "foreign_listener",
        live_annotation: {
          blocked: true,
          text: "server holds ? MiB, live annotation cannot start",
        },
      }),
    );
    expect(say(liveSay(unknown)!)).toContain("how much is not known");
    expect(
      liveSay(status(idle({ live_annotation: { blocked: false } }))),
    ).toBeNull();
  });
});

describe("checks", () => {
  test("every check the backend lists has a title and an ok sentence in both languages", () => {
    expect(CHECKS.length).toBe(9);
    const rows = checkRows(status());
    expect(rows.map((r) => r.code)).toEqual(CHECKS);
    for (const row of rows) {
      expect(row.level).toBe("ok");
      for (const lang of ["en", "zh"] as const) {
        expect(say({ key: row.title }, lang), row.code).not.toMatch(
          /^marvel0\./,
        );
        expect(worded(row.say, lang), row.code).not.toMatch(/^marvel0\./);
      }
    }
  });

  test("a refused check is worded as the refusal of a deploy would be", () => {
    const rows = checkRows(
      status(
        idle({
          checks: checks({
            port_free: {
              level: "refuse",
              text: "English",
              params: { port: 8000, listeners: 1, ours: false },
            },
            vram: {
              level: "refuse",
              text: "English",
              params: {
                fraction: 0.22,
                total_mib: 32607,
                free_mib: 5100,
                required_mib: 8198,
                reserved_mib: 7174,
                margin_mib: 1024,
              },
            },
            arm_lock: { level: "refuse", text: "English", params: {} },
            no_client: {
              level: "refuse",
              text: "English",
              params: { established: 2 },
            },
          }),
        }),
      ),
    );
    const by = Object.fromEntries(rows.map((r) => [r.code, r]));
    expect(by.port_free.level).toBe("refuse");
    expect(worded(by.port_free.say)).toContain("port 8000");
    expect(worded(by.vram.say)).toContain("8198");
    expect(worded(by.arm_lock.say)).toContain("arm lock is held");
    expect(worded(by.no_client.say)).toContain("2 connection");
  });

  test("a port held by the unit of this page, and a unit of this page, say it is already running", () => {
    const rows = checkRows(
      status(
        idle({
          checks: checks({
            port_free: { level: "refuse", text: "English", params: {} },
            policy_units: {
              level: "refuse",
              text: "English",
              params: { units: [`${UNIT}.service`] },
            },
          }),
        }),
      ),
    );
    const by = Object.fromEntries(rows.map((r) => [r.code, r]));
    expect(worded(by.port_free.say)).toContain("already running");
    expect(worded(by.policy_units.say)).toContain("already running");
    const other = checkRows(
      status(
        idle({
          checks: checks({
            policy_units: {
              level: "refuse",
              text: "English",
              params: { units: ["levi-aeri-policy-forward.service"] },
            },
          }),
        }),
      ),
    ).find((r) => r.code === "policy_units")!;
    expect(worded(other.say)).toContain("levi-aeri-policy-forward.service");
    expect(worded(other.say)).not.toContain("already running");
  });

  test("a check that could not be read says so, with the right words for what it reads", () => {
    const rows = checkRows(
      status(
        idle({
          checks: checks({
            net_table: { level: "unknown", text: "E", params: {} },
            port_free: { level: "unknown", text: "E", params: {} },
            policy_units: { level: "unknown", text: "E", params: {} },
            arm_lock: { level: "unknown", text: "E", params: {} },
            gpu_lock: { level: "unknown", text: "E", params: {} },
            gpu_readable: { level: "unknown", text: "E", params: {} },
            vram: { level: "unknown", text: "E", params: {} },
          }),
        }),
      ),
    );
    const by = Object.fromEntries(rows.map((r) => [r.code, r]));
    for (const row of rows.filter((r) => r.level === "unknown"))
      expect(worded(row.say), row.code).not.toBeNull();
    expect(worded(by.net_table.say)).toContain("socket tables");
    expect(worded(by.policy_units.say)).toContain("systemd");
    expect(worded(by.arm_lock.say)).toContain("arm lock");
    expect(worded(by.gpu_lock.say)).toContain("GPU lock");
    expect(worded(by.vram.say)).toContain("GPU");
  });

  test("a check the page does not know keeps the server's sentence", () => {
    const rows = checkRows(
      status(
        idle({
          checks: [
            {
              code: "new_check",
              level: "refuse",
              text: "Server text",
              params: {},
            },
          ],
        }),
      ),
    );
    expect(rows[0].title).toBeNull();
    expect(worded(rows[0].say)).toBe("Server text");
  });
});

describe("notes", () => {
  test("the unmanifested source-hash note is worded; an unknown note is as written", () => {
    expect(
      worded(noteSay({ code: "source_hash_unmanifested", text: "E" })),
    ).toContain("sha256");
    expect(noteSay({ code: "x", text: "Plain" })).toEqual({
      say: null,
      text: "Plain",
    });
  });
});

describe("the form", () => {
  test("starts on the first model, RTC as the server defaults, the default fraction", () => {
    expect(initialForm(models, site)).toEqual({
      modelId: "pi05_fr3_all_step49999",
      rtc: true,
      fraction: 0.22,
      scale: "",
      acceptUnverified: false,
    });
  });

  test("a default fraction that is not a preset falls back to the first preset", () => {
    const view = readModels(
      modelsAnswer({
        defaults: { rtc: false, fraction: 0.5, cfg_guidance_scale: 1 },
      }),
    )!;
    expect(initialForm(view, site)).toMatchObject({
      rtc: false,
      fraction: 0.22,
    });
  });

  test("with no models there is no model and no fraction is guessed without presets", () => {
    const empty = readModels(modelsAnswer({ models: [] }))!;
    expect(initialForm(empty, site).modelId).toBe("");
    const noPresets = { ...site, gpu: { ...site.gpu, presets: [] } };
    expect(initialForm(models, noPresets).fraction).toBeNull();
  });
});

describe("resolveForm", () => {
  const initial = initialForm(models, site);

  test("the person's edits are over the opening values", () => {
    expect(
      resolveForm(
        initial,
        { modelId: cfg.id, fraction: 0.3, rtc: false },
        models,
        site,
      ),
    ).toEqual({ ...initial, modelId: cfg.id, fraction: 0.3, rtc: false });
  });

  test("a model that left the list falls back to the first", () => {
    expect(
      resolveForm(initial, { modelId: "gone" }, models, site).modelId,
    ).toBe(sft.id);
  });

  test("a fraction that left the presets falls back to the opening one", () => {
    expect(resolveForm(initial, { fraction: 0.5 }, models, site).fraction).toBe(
      0.22,
    );
    expect(
      resolveForm(initial, { fraction: null }, models, site).fraction,
    ).toBe(0.22);
  });
});

describe("newRequestId", () => {
  const ID = /^[A-Za-z0-9._:-]{8,80}$/;

  test("is an id the server accepts, and a new one each time", () => {
    const a = newRequestId();
    expect(a).toMatch(ID);
    expect(newRequestId()).not.toBe(a);
  });

  test("still is one where randomUUID is missing (a page that is not a secure context)", () => {
    const real = globalThis.crypto.randomUUID;
    Object.defineProperty(globalThis.crypto, "randomUUID", {
      configurable: true,
      value: undefined,
    });
    try {
      const a = newRequestId();
      expect(a).toMatch(ID);
      expect(newRequestId()).not.toBe(a);
    } finally {
      Object.defineProperty(globalThis.crypto, "randomUUID", {
        configurable: true,
        value: real,
      });
    }
  });
});

describe("the guidance scale", () => {
  test("a model without guidance takes none", () => {
    expect(parseScale("1.2", sft, site)).toEqual({ value: null, error: false });
  });

  test("empty means the server's default", () => {
    expect(parseScale("", cfg, site)).toEqual({ value: null, error: false });
    expect(parseScale("  ", cfg, site)).toEqual({ value: null, error: false });
  });

  test("a number above 0 and at most the site's maximum is taken", () => {
    expect(parseScale("1.2", cfg, site)).toEqual({ value: 1.2, error: false });
    expect(parseScale("1.5", cfg, site)).toEqual({ value: 1.5, error: false });
  });

  test("anything else is an error", () => {
    for (const bad of [
      "0",
      "-1",
      "1.6",
      "abc",
      "1,2",
      "NaN",
      "Infinity",
      "1e999",
    ])
      expect(parseScale(bad, cfg, site), bad).toEqual({
        value: null,
        error: true,
      });
  });
});

describe("why Deploy is off", () => {
  const block = (
    data: Record<string, unknown>,
    over: Partial<DeployForm> = {},
    options: { pending?: boolean; model?: typeof sft | null } = {},
  ) =>
    worded(
      deployBlock(
        readDeploy(data),
        form(over),
        options.model === undefined ? sft : options.model,
        site,
        options.pending ?? false,
      ),
    );

  test("nothing blocks an idle marvel0 with room", () => {
    expect(block(idle())).toBeNull();
  });

  test("no state, a press in flight, a running operation", () => {
    expect(deployBlock(null, form(), sft, site, false)?.say).toEqual({
      key: "marvel0.deploy.block.no_state",
    });
    expect(block(idle(), {}, { pending: true })).toContain(
      "deploy or stop is running",
    );
    expect(
      block(
        idle({
          operation: { action: "stop", request_id: "x", started_at: "t" },
        }),
      ),
    ).toContain("deploy or stop is running");
  });

  test("a server that runs or is stopping blocks, naming its state", () => {
    expect(block(starting())).toBe(
      "Deploy is off: the policy server is Starting.",
    );
    expect(block(ready())).toContain("Ready");
    expect(block(idle({ state: "stopping", can_deploy: false }))).toContain(
      "Stopping",
    );
    expect(
      block(idle({ state: "foreign_listener", can_deploy: false })),
    ).toContain("Started by hand");
    expect(block(idle({ state: "unknown", can_deploy: false }))).toContain(
      "Unknown",
    );
  });

  test("a failed or exited server does not block a new deploy by itself", () => {
    expect(block(idle({ state: "failed" }))).toBeNull();
    expect(block(idle({ state: "dead" }))).toBeNull();
  });

  test("a check that is not ok blocks, in the words of its refusal", () => {
    const text = block(
      idle({
        can_deploy: false,
        checks: checks({
          arm_lock: { level: "refuse", text: "E", params: {} },
        }),
      }),
    );
    expect(text).toContain("Deploy is off:");
    expect(text).toContain("arm lock is held");
  });

  test("no model, no fraction", () => {
    expect(block(idle(), {}, { model: null })).toBe("No model is selected.");
    expect(block(idle(), { fraction: null })).toBe(
      "No VRAM fraction is selected.",
    );
    expect(block(idle(), { fraction: 0.5 })).toBe(
      "No VRAM fraction is selected.",
    );
  });

  test("a fraction the card cannot hold blocks and says by how much", () => {
    const small = idle({
      gpu: {
        state: "ok",
        total_mib: 32607,
        used_mib: 27507,
        free_mib: 5100,
        server_holds_mib: null,
      },
      vram: {
        margin_mib: 1024,
        presets: [
          { fraction: 0.22, required_mib: 8198, enough: true },
          { fraction: 0.3, required_mib: 10807, enough: false },
          { fraction: 0.45, required_mib: 15698, enough: false },
        ],
        hint: "x",
      },
    });
    expect(block(small, { fraction: 0.22 })).toBeNull();
    const text = block(small, { fraction: 0.3 })!;
    expect(text).toContain("0.30");
    expect(text).toContain("10807");
    expect(text).toContain("5100");
  });

  test("a card that cannot be read blocks every fraction", () => {
    const unreadable = idle({
      can_deploy: false,
      gpu: {
        state: "unknown",
        total_mib: null,
        used_mib: null,
        free_mib: null,
      },
      checks: checks({
        gpu_readable: { level: "unknown", text: "E", params: {} },
      }),
      vram: {
        margin_mib: 1024,
        presets: [{ fraction: 0.22, required_mib: null, enough: null }],
        hint: null,
      },
    });
    expect(block(unreadable)).toContain("GPU");
  });

  test("a guidance scale that is out of range blocks", () => {
    expect(
      block(idle(), { modelId: cfg.id, scale: "9" }, { model: cfg }),
    ).toContain("1.5");
    expect(
      block(idle(), { modelId: cfg.id, scale: "1.2" }, { model: cfg }),
    ).toBeNull();
  });

  test("a server that says no without a reason the page can name still blocks", () => {
    expect(block(idle({ can_deploy: false }))).toBe(
      "The server does not offer a deploy right now.",
    );
  });
});

describe("the requests", () => {
  test("a deploy body carries the id, the choices and the typed phrase, nothing else", () => {
    const body = deployBody(
      "req-12345678",
      form(),
      sft,
      "deploy pi05_fr3_all_step49999",
    );
    expect(body).toEqual({
      request_id: "req-12345678",
      model_id: "pi05_fr3_all_step49999",
      rtc: true,
      inference_mode: "rtc",
      fraction: 0.22,
      cfg_guidance_scale: null,
      accept_unverified: false,
      confirm: "deploy pi05_fr3_all_step49999",
    });
  });

  test("the guidance scale goes only with a model that takes one", () => {
    const withScale = form({
      modelId: cfg.id,
      scale: "1.2",
      acceptUnverified: true,
    });
    expect(deployBody("r", withScale, cfg, "p")).toMatchObject({
      cfg_guidance_scale: 1.2,
      accept_unverified: true,
    });
    expect(
      deployBody("r", form({ scale: "1.2" }), sft, "p").cfg_guidance_scale,
    ).toBeNull();
    expect(
      deployBody("r", form({ modelId: cfg.id, scale: "" }), cfg, "p")
        .cfg_guidance_scale,
    ).toBeNull();
  });

  test("an acceptance left ticked is not sent for a model that is verified", () => {
    expect(
      deployBody("r", form({ acceptUnverified: true }), sft, "p")
        .accept_unverified,
    ).toBe(false);
  });

  test("the phrases come from the server, with the model filled in", () => {
    expect(deployPhrase(status(), "m1")).toBe("deploy m1");
    expect(
      deployPhrase(
        status(
          idle({
            confirm: {
              deploy: "launch {model_id}",
              stop: "halt",
              client_override: "force",
            },
          }),
        ),
        "m1",
      ),
    ).toBe("launch m1");
    expect(stopPhrase(status())).toBe("stop policy server");
    expect(overridePhrase(status())).toBe("stop with client connected");
    expect(deployPhrase(null, "m1")).toBe("deploy m1");
    expect(stopPhrase(null)).toBe("stop policy server");
  });

  test("a stop body sends the override only when it was typed exactly", () => {
    expect(stopBody("r", "stop policy server", "")).toEqual({
      request_id: "r",
      confirm: "stop policy server",
      client_override: "",
    });
    expect(
      stopBody(
        "r",
        "stop policy server",
        "stop with client connected",
        "stop with client connected",
      ).client_override,
    ).toBe("stop with client connected");
    expect(
      stopBody(
        "r",
        "stop policy server",
        "stop with",
        "stop with client connected",
      ).client_override,
    ).toBe("");
  });
});

describe("the Stop control", () => {
  test("is shown while the server says a stop is possible", () => {
    expect(stopControl(status(ready())).show).toBe(true);
    expect(stopControl(status(starting())).show).toBe(true);
    expect(stopControl(status()).show).toBe(false);
    expect(stopControl(null).show).toBe(false);
  });

  test("a server started by hand has no Stop, only the instruction", () => {
    const hand = stopControl(
      status(
        idle({
          state: "foreign_listener",
          can_stop: false,
          reason: { code: "hand_started", text: "x" },
        }),
      ),
    );
    expect(hand.show).toBe(false);
    expect(hand.byHand).toBe(true);
  });

  test("counts the clients connected", () => {
    const busy = stopControl(
      status(
        ready({
          listener: { port: 8000, present: true, ours: true, established: 2 },
          stop_blockers: ["client_connected"],
        }),
      ),
    );
    expect(busy.show).toBe(true);
    expect(busy.clients).toBe(2);
    expect(stopControl(status(ready())).clients).toBe(0);
  });

  test("an unreadable socket table counts as a client being possible", () => {
    const unknown = stopControl(
      status(
        ready({
          stop_blockers: ["net_unreadable"],
          listener: {
            port: 8000,
            present: null,
            ours: null,
            established: null,
          },
        }),
      ),
    );
    expect(unknown.clients).toBeNull();
  });

  test("is off while an operation runs", () => {
    const running = stopControl(
      status(
        ready({
          operation: { action: "deploy", request_id: "x", started_at: "t" },
        }),
      ),
    );
    expect(running.show).toBe(true);
    expect(running.operation).toBe(true);
  });
});

describe("what a write answered", () => {
  test("a deploy that was accepted says how much memory it needs", () => {
    const result = deployResult(deployed())!;
    expect(result.tone).toBe("success");
    expect(result.lines.map((l) => say(l))).toEqual([
      "Deploy accepted: the policy server is starting. It needs 8198 MiB free; 23100 MiB were free at the final check.",
    ]);
  });

  test("a deploy asked twice is said to have started once", () => {
    const result = deployResult(deployed({ replayed: true }))!;
    expect(say(result.lines[0])).toContain("nothing was started again");
  });

  test("a request that was accepted and never answered starts nothing and says to read the state", () => {
    const result = deployResult({
      schema: "levi.marvel0.deploy_result.v1",
      created: false,
      replayed: true,
      request_id: "x",
      state: "unknown",
      reason: { code: "request_incomplete", text: "E" },
    })!;
    expect(result.tone).toBe("warning");
    expect(say(result.lines[0])).toContain("never answered");
  });

  test("an answer that is not one of these is no result", () => {
    expect(deployResult(null)).toBeNull();
    expect(deployResult({ schema: "other" })).toBeNull();
    expect(stopResult("x")).toBeNull();
  });

  test("a stop that finished, one that found nothing, one still going", () => {
    expect(say(stopResult(stopped())!.lines[0])).toContain("is stopped");
    expect(
      say(stopResult(stopped({ stopped: false, already: true }))!.lines[0]),
    ).toContain("nothing left to stop");
    const going = stopResult(stopped({ state: "stopping", stopped: false }))!;
    expect(say(going.lines[0])).toContain("has not finished");
    expect(going.tone).toBe("warning");
  });

  test("a stop that cut a client off says to keep a hand on the e-stop", () => {
    const forced = stopResult(
      stopped({ client_override_used: true, warning: "English" }),
    )!;
    expect(forced.lines).toHaveLength(2);
    expect(say(forced.lines[1])).toContain("e-stop");
    expect(forced.tone).toBe("warning");
  });
});

describe("polling", () => {
  test("the log is asked for while a deploy is starting, failed, exited, stopping or looked at", () => {
    expect(wantLog(status(starting()), false)).toBe(true);
    expect(
      wantLog(status(idle({ state: "failed", deploy: deployInfo() })), false),
    ).toBe(true);
    expect(
      wantLog(status(idle({ state: "dead", deploy: deployInfo() })), false),
    ).toBe(true);
    expect(
      wantLog(status(idle({ state: "stopping", deploy: deployInfo() })), false),
    ).toBe(true);
    expect(wantLog(status(ready()), false)).toBe(false);
    expect(wantLog(status(ready()), true)).toBe(true);
  });

  test("no log is asked for without a deploy record or a state", () => {
    expect(wantLog(status(), true)).toBe(false);
    expect(wantLog(null, true)).toBe(false);
  });

  test("the wait doubles after each failure up to 30 s", () => {
    expect(deployDelay(0)).toBe(3000);
    expect(deployDelay(1)).toBe(6000);
    expect(deployDelay(2)).toBe(12000);
    expect(deployDelay(3)).toBe(24000);
    expect(deployDelay(4)).toBe(30000);
    expect(deployDelay(50)).toBe(30000);
    expect(deployDelay(2, 20)).toBe(80);
  });
});

describe("readLog", () => {
  test("reads the lines of the log as written", () => {
    expect(readLog(logAnswer())).toEqual({
      logId: `${UNIT}-20261019T031200Z.log`,
      sizeBytes: 18234,
      lines: [
        "INFO:root:Using norm stats shipped with the checkpoint: norm_stats.json",
        "INFO:root:RTC enabled: max_guidance_weight=5.0, schedule=exp, plain policy",
      ],
    });
  });

  test("no log yet is an empty list, not an error", () => {
    expect(
      readLog(logAnswer({ log_id: null, size_bytes: 0, lines: [] })),
    ).toEqual({
      logId: null,
      sizeBytes: 0,
      lines: [],
    });
  });

  test("only text lines are kept, bounded", () => {
    const view = readLog(
      logAnswer({ lines: ["a", 3, null, "b".repeat(1000)] }),
    )!;
    expect(view.lines).toHaveLength(2);
    expect(view.lines[1]).toHaveLength(400);
  });

  test("another schema, or no list, is no log", () => {
    expect(readLog(logAnswer({ schema: "x" }))).toBeNull();
    expect(readLog(logAnswer({ lines: "x" }))).toBeNull();
    expect(readLog(null)).toBeNull();
  });
});
