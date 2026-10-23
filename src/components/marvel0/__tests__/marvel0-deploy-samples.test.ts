// The answers of the marvel0 backend on its fake machine (levi/marvel0/deploy.py,
// catalog_api.py), read by the page's own parsers: the shapes the frontend
// fixtures imitate are the servers' real ones. marvel0-deploy.samples.json is made
// by running the backend in process (the fake machine of
// tests/marvel0/test_marvel0_deploy.py, the catalog router on a test client); no
// process, socket or real file of the machine is involved.
//
// Regenerate the file with
//   uv run --no-sync python tests/marvel0/regen_deploy_sample.py
// tests/marvel0/test_marvel0_deploy_sample.py fails, naming that command, when the
// backend's answers no longer match the committed file: a field it renames,
// drops or adds is seen there first, then here.
import { describe, expect, test } from "bun:test";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import { errorOf } from "../deploy-api";
import { readModels, readSite } from "../catalog-logic";
import { readPreview } from "../command-logic";
import {
  checkRows,
  deployBlock,
  deployResult,
  failureSay,
  initialForm,
  liveSay,
  presetRows,
  readDeploy,
  readLog,
  readinessRows,
  reasonSay,
  shownDeploy,
  stopControl,
  stopResult,
  type DeployStatus,
} from "../deploy-logic";
import { fieldMessages, sayFailure } from "../marvel0-failure";
import { sayText } from "../marvel0-say";
import type { Worded } from "../marvel0-json";
import type { Say } from "../marvel0-logic";
import samples from "./marvel0-deploy.samples.json";

const S = samples as Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any
const catalogue = (lang: "en" | "zh") => (key: string) =>
  (lang === "en" ? en : zh)[key as keyof typeof en] ?? key;
const say = (value: Say, lang: "en" | "zh" = "en") =>
  sayText(value, catalogue(lang));
const worded = (value: Worded<Say> | null, lang: "en" | "zh" = "en") =>
  value ? (value.say ? say(value.say, lang) : `RAW:${value.text}`) : null;
const status = (name: string) => readDeploy(S[name]) as DeployStatus;
const models = readModels(S.models)!;
const site = readSite(S.site)!;
const form = initialForm(models, site);

describe("the backend's own answers, read by the page", () => {
  test("every state sample is read, and none of its checks falls back to the server's English", () => {
    for (const name of [
      "idle",
      "starting",
      "ready",
      "ready_with_client",
      "failed_oom",
      "foreign",
      "ready_cfg",
      "blocked_arm",
      "idle_after_stop",
    ]) {
      const s = status(name);
      expect(s, name).not.toBeNull();
      for (const row of checkRows(s))
        for (const lang of ["en", "zh"] as const)
          expect(worded(row.say, lang), `${name} ${row.code}`).not.toMatch(
            /^RAW:/,
          );
      for (const word of [reasonSay(s), failureSay(s)])
        if (word) expect(word.say, name).not.toBeNull();
    }
  });

  test("idle: nothing runs, every fraction fits, Deploy is not blocked", () => {
    const s = status("idle");
    expect(s.state).toBe("idle");
    expect(
      presetRows(s).map((r) => [r.label, r.requiredMib, r.enough]),
    ).toEqual([
      ["0.22", 8198, true],
      ["0.30", 10807, true],
      ["0.45", 15698, true],
    ]);
    expect(deployBlock(s, form, models.models[0], site, false)).toBeNull();
    expect(stopControl(s).show).toBe(false);
  });

  test("starting: the log has said nothing yet, the server holds 7174 MiB", () => {
    const s = status("starting");
    expect(readinessRows(s).map((r) => [r.id, r.value])).toEqual([
      ["norm_stats", false],
      ["rtc_enabled", false],
      ["rtc_warmup", false],
      ["listening", false],
    ]);
    expect(say(liveSay(s)!)).toContain("7174 MiB");
    expect(stopControl(s).show).toBe(true);
    const policy = checkRows(s).find((r) => r.code === "policy_units")!;
    expect(worded(policy.say)).toContain("already running");
    expect(
      worded(deployBlock(s, form, models.models[0], site, false)),
    ).toContain("Starting");
  });

  test("ready: every line the log should have, and no client", () => {
    const s = status("ready");
    expect(readinessRows(s).every((r) => r.value === true)).toBe(true);
    expect(stopControl(s)).toEqual({
      show: true,
      byHand: false,
      clients: 0,
      operation: false,
    });
    const port = checkRows(s).find((r) => r.code === "port_free")!;
    expect(worded(port.say)).toContain("already running");
  });

  test("a client connected is counted from the socket table", () => {
    const s = status("ready_with_client");
    expect(stopControl(s).clients).toBe(2);
    const row = checkRows(s).find((r) => r.code === "no_client")!;
    expect(worded(row.say)).toContain("2 connection");
  });

  test("a guidance deploy lists the guidance line", () => {
    const rows = readinessRows(status("ready_cfg"));
    expect(rows.map((r) => r.id)).toEqual([
      "norm_stats",
      "rtc_enabled",
      "rtc_warmup",
      "cfg",
      "listening",
    ]);
    expect(rows.every((r) => r.value === true)).toBe(true);
  });

  test("a deploy that ran out of memory is a failure in words, and a new deploy is allowed", () => {
    const s = status("failed_oom");
    expect(worded(failureSay(s))).toContain("ran out of memory");
    expect(worded(failureSay(s), "zh")).toMatch(/[一-鿿]/);
    expect(deployBlock(s, form, models.models[0], site, false)).toBeNull();
    expect(stopControl(s).show).toBe(false);
    const log = readLog(S.log_oom)!;
    expect(log.lines.join("\n")).toContain("RESOURCE_EXHAUSTED");
  });

  test("a server started by hand: no Stop, the instruction to press Ctrl+C, memory held unknown", () => {
    const s = status("foreign");
    expect(s.state).toBe("foreign_listener");
    expect(stopControl(s).show).toBe(false);
    expect(stopControl(s).byHand).toBe(true);
    expect(worded(reasonSay(s))).toContain("press Ctrl+C");
    expect(say(liveSay(s)!)).toContain("how much is not known");
    const port = checkRows(s).find((r) => r.code === "port_free")!;
    expect(worded(port.say)).toContain("port 8000");
  });

  test("the arm lock held by a run blocks a deploy in words", () => {
    const s = status("blocked_arm");
    expect(
      worded(deployBlock(s, form, models.models[0], site, false)),
    ).toContain("arm lock is held");
  });

  test("an idle marvel0 after a stop says nothing about the deploy that ended", () => {
    const s = status("idle_after_stop");
    expect(s.deploy).not.toBeNull();
    expect(shownDeploy(s)).toBeNull();
    expect(readinessRows(s)).toEqual([]);
    expect(deployBlock(s, form, models.models[0], site, false)).toBeNull();
  });

  test("the answers of the two writes", () => {
    const started = deployResult(S.deployed)!;
    expect(say(started.lines[0])).toBe(
      "Deploy accepted: the policy server is starting. It needs 8198 MiB free; 23507 MiB were free at the final check.",
    );
    expect(say(stopResult(S.stopped)!.lines[0])).toContain("is stopped");
  });

  test("the refusals the backend gave are worded with their facts", () => {
    const refused = (name: string) => {
      const sample = S.refusals[name];
      return errorOf(sample.status, { detail: sample.detail });
    };
    expect(worded(sayFailure(refused("gpu_insufficient")))).toContain(
      "needs 10807 MiB free (9783 + 1024); 5100 MiB are free",
    );
    expect(worded(sayFailure(refused("port_taken")))).toContain("port 8000");
    expect(worded(sayFailure(refused("unverified_not_accepted")))).toContain(
      "no recorded real-GPU serving check",
    );
    expect(worded(sayFailure(refused("already_running")))).toContain(
      "already running",
    );
    for (const name of Object.keys(S.refusals))
      expect(worded(sayFailure(refused(name)), "zh"), name).toMatch(/[一-鿿]/);
  });

  test("the missing site file names the path it looked at", () => {
    const failure = errorOf(S.site_missing.status, S.site_missing.body);
    expect(failure.code).toBe("site_missing");
    expect(worded(sayFailure(failure))).toContain("/path/to/none.toml");
    expect(String(failure.extra.template)).toContain("levi.marvel0.site.v1");
  });
});

describe("the catalog, the site and the preview as the backend writes them", () => {
  test("the four deployable models, and why the others are left out", () => {
    expect(models.models.map((m) => m.id)).toEqual([
      "pi05_fr3_all_step49999",
      "recap_cfg_r1_step10000_jax",
      "recap_cfg_r2_best_step14300_jax",
      "recap_cfg_r2_step15000_jax",
    ]);
    expect(models.models.map((m) => m.verified)).toEqual([
      true,
      false,
      false,
      false,
    ]);
    expect(models.models.filter((m) => m.cfgCapable)).toHaveLength(3);
    expect(models.skipped.map((s) => s.reason)).toContain("excluded_by_site");
  });

  test("the site view has what the forms need", () => {
    expect(site.caps.maxSteps.cap).toBeGreaterThan(0);
    expect(site.gpu.presets).toEqual([0.22, 0.3, 0.45]);
    expect(site.prompts.known.length).toBeGreaterThan(0);
    expect(site.fixedFlags.length).toBeGreaterThan(5);
  });

  test("the preview is a command and its needs", () => {
    const preview = readPreview(S.preview)!;
    expect(preview.command).toContain("--min-z=");
    expect(preview.command).toContain("--levi-mode=off");
    expect(preview.needs).toEqual({
      modelId: "pi05_fr3_all_step49999",
      rtc: true,
      inferenceMode: "rtc",
    });
    expect(preview.command).not.toContain("/tmp/");
  });

  test("the warnings of a preview are the ones the page words", () => {
    const preview = readPreview(S.preview_warn)!;
    expect(preview.warnings.map((w) => w.code).sort()).toEqual([
      "min_z_below_task_table",
      "rtc_margin_low",
      "unverified_model",
    ]);
  });

  test("the refusal of min-z lands on its field", () => {
    const failure = errorOf(S.preview_min_z.status, S.preview_min_z.body);
    const out = fieldMessages(failure, { site });
    expect(Object.keys(out)).toEqual(["min_z"]);
    expect(worded(out.min_z)).toContain("0.025");
  });

  test("FastAPI's refusal of a body lands on its field", () => {
    const failure = errorOf(S.preview_type.status, S.preview_type.body);
    expect(Object.keys(fieldMessages(failure, { site }))).toEqual([
      "max_steps",
    ]);
  });
});
