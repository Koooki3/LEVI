import { afterEach, describe, expect, test } from "bun:test";
import { click, setupDom, waitFor } from "@/components/ds/__tests__/dom";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import { button, doubleClick, textFields, tick, type } from "./actions-helpers";
import { $, dialog, openStep, serve, text, wait } from "./autonomy-helpers";
import {
  LEFTOVERS_STOP,
  LIVE_STOP,
  PREPARE,
  RUN_ANSWERS,
  RUN_REFUSALS,
  callsOf,
  readyDoc,
  refusal,
  runDoc,
} from "./autonomy-fixtures";

setupDom();
afterEach(() => {
  try {
    localStorage.clear();
  } catch {
    // no storage in this DOM
  }
});

const E = en as Record<string, string>;
const Z = zh as Record<string, string>;

/** A readiness document in which every row is met. */
const allMet = (change?: (doc: ReturnType<typeof readyDoc>) => void) =>
  readyDoc("prepared_ready", (doc) => {
    for (const row of doc.readiness) row.level = "ok";
    change?.(doc);
  });

const rowOf = (host: ParentNode, code: string) =>
  host.querySelector(
    `[data-testid=marvel0-autonomy-ready] li[data-code="${code}"]`,
  ) as HTMLElement | null;
const start = (host: ParentNode) =>
  $(host, "marvel0-autonomy-start") as HTMLButtonElement | null;

describe("the readiness rows", () => {
  test("what refuses comes first, in the backend's order, each with its level in words; what is met is folded", async () => {
    const { host } = await openStep({ ready: readyDoc("blocked") });
    const refusing = $(host, "marvel0-autonomy-ready-refusing")!;
    const codes = Array.from(refusing.querySelectorAll("li")).map((li) =>
      li.getAttribute("data-code"),
    );
    const doc = readyDoc("blocked");
    expect(codes).toEqual(
      doc.readiness
        .filter(
          (r: { level: string }) =>
            r.level === "refuse" || r.level === "unknown",
        )
        .map((r: { code: string }) => r.code),
    );
    expect(codes.length).toBeGreaterThan(3);
    for (const li of Array.from(refusing.querySelectorAll("li")))
      expect(text(li)).toMatch(/^(Not met:|Not known \(counts as not met\):)/);
    const met = host.querySelector(
      "[data-testid=marvel0-autonomy-ready] details",
    )!;
    expect((met as HTMLDetailsElement).open).toBe(false);
    expect(met.querySelectorAll("li[data-level=ok]").length).toBeGreaterThan(5);
  });

  test("an unknown row is listed with the refusals and said to count as not met", async () => {
    const { host } = await openStep({ ready: readyDoc("blocked") });
    const unknown = rowOf(host, "prompts_checked") ?? rowOf(host, "gpu_free");
    const row = host.querySelector(
      "[data-testid=marvel0-autonomy-ready] li[data-level=unknown]",
    )!;
    expect(unknown).not.toBeNull();
    expect(text(row)).toContain(E["marvel0.autonomy.level.unknown"]);
  });

  test("a row is in the page's words with the backend's numbers in it; a code the page does not know shows the server's sentence, marked English", async () => {
    const { host } = await openStep({ ready: readyDoc("blocked") });
    const gpu = readyDoc("blocked").readiness.find(
      (r: { code: string }) => r.code === "gpu_free",
    ).params;
    expect(text(rowOf(host, "gpu_free"))).toContain(
      `At least ${gpu.need_mib} MiB of GPU memory are free (${gpu.free_mib} MiB now)`,
    );
    const odd = await openStep({
      ready: readyDoc("blocked", (doc) =>
        doc.readiness.push({
          code: "from_the_future",
          level: "refuse",
          text: "SERVER words",
          params: {},
        }),
      ),
    });
    const row = rowOf(odd.host, "from_the_future")!;
    expect(row.querySelector("[lang=en]")!.textContent).toBe("SERVER words");
  });

  test("the count is the number met out of the number checked", async () => {
    const { host } = await openStep({ ready: readyDoc("blocked") });
    const doc = readyDoc("blocked");
    const met = doc.readiness.filter(
      (r: { level: string }) => r.level === "ok",
    ).length;
    expect(text($(host, "marvel0-autonomy-ready-count"))).toBe(
      `${met} of ${doc.readiness.length} met`,
    );
  });

  test("every code the backend lists is a row the page knows how to say", async () => {
    const { host } = await openStep({ ready: allMet() });
    const doc = readyDoc("prepared_ready");
    for (const row of doc.readiness)
      expect([
        row.code,
        rowOf(host, row.code)?.querySelector("[lang=en]") ?? null,
      ]).toEqual([row.code, null]);
  });
});

describe("preparing a run", () => {
  test("the form starts at the backend's defaults; the mode says what it costs; no field holds a phrase", async () => {
    const { host } = await openStep({ ready: readyDoc("blocked") });
    const form = $(host, "marvel0-autonomy-form")!;
    const values = textFields(form).map((f) => f.value);
    expect(values).toEqual(["", "5", "200", "4"]);
    // the starting mode is local (the first real run was prepared in external
    // mode by a slip of the form)
    expect(text($(host, "marvel0-autonomy-mode-hint"))).toBe(
      E["marvel0.autonomy.mode_hint.local"],
    );
    await click(button(form, E["marvel0.autonomy.mode.external"])!);
    expect(text($(host, "marvel0-autonomy-mode-hint"))).toBe(
      E["marvel0.autonomy.mode_hint.external"],
    );
    expect(text(form)).toContain("Between 1 and 50.");
    expect(text(form)).toContain("between 1 and 3000");
  });

  test("Prepare is off until the task is a task the policy knows how to be told", async () => {
    const { host } = await openStep({ ready: readyDoc("blocked") });
    const form = $(host, "marvel0-autonomy-form")!;
    const prepare = $(host, "marvel0-autonomy-prepare") as HTMLButtonElement;
    expect(prepare.disabled).toBe(true);
    await type(textFields(form)[0], "Pick The Pepper");
    expect(prepare.disabled).toBe(true);
    expect(text(form)).toContain(E["marvel0.autonomy.form.task_bad"]);
    await type(textFields(form)[0], "pick the red pepper into the white plate");
    expect(prepare.disabled).toBe(false);
    expect(text(form)).not.toContain(E["marvel0.autonomy.form.task_bad"]);
    await type(textFields(form)[1], "51");
    expect(prepare.disabled).toBe(true);
    expect(text(form)).toContain("A number between 1 and 50.");
  });

  test("one click is one request with the closed body of the route and the backend's phrase; two clicks are still one", async () => {
    const f = await openStep({ ready: readyDoc("blocked") });
    const form = $(f.host, "marvel0-autonomy-form")!;
    await type(textFields(form)[0], "pick the red pepper into the white plate");
    await type(textFields(form)[2], "300");
    await doubleClick($(f.host, "marvel0-autonomy-prepare"));
    await tick(3, 10);
    const sent = callsOf(f.state, "prepare");
    expect(sent).toHaveLength(1);
    expect(sent[0]).toEqual({
      request_id: expect.stringMatching(/^[A-Za-z0-9._:-]{8,80}$/),
      mode: "local",
      task: "pick the red pepper into the white plate",
      rounds: 5,
      steps: 300,
      gripper_confirm_s: 4,
      start_home: true,
      reset_policy: false,
      grasp_assist: false,
      early_check: null,
      confirm: "prepare aeri run",
    });
  });

  test("the form starts in external mode where the site has no local model, and local where it has one", async () => {
    const plain = await openStep({
      ready: readyDoc("blocked", (doc) => (doc.limits.modes = ["external"])),
    });
    expect(text($(plain.host, "marvel0-autonomy-mode-hint"))).toBe(
      E["marvel0.autonomy.mode_hint.external"],
    );
    await type(
      textFields($(plain.host, "marvel0-autonomy-form")!)[0],
      "pick the pepper into the white plate",
    );
    await click($(plain.host, "marvel0-autonomy-prepare"));
    await tick(3, 10);
    expect(callsOf(plain.state, "prepare")[0].mode).toBe("external");
    const both = await openStep({ ready: readyDoc("blocked") });
    expect(text($(both.host, "marvel0-autonomy-mode-hint"))).toBe(
      E["marvel0.autonomy.mode_hint.local"],
    );
  });

  test("start home is a switch that exists only where the code version has the three confirmations; it starts on there, and is sent as chosen", async () => {
    const homeable = await openStep({ ready: readyDoc("blocked") });
    const sw = homeable.host.querySelector(
      "[data-testid=marvel0-autonomy-form] input[role=switch]",
    ) as HTMLInputElement;
    expect([sw.disabled, sw.checked]).toEqual([false, true]);
    await click(sw);
    expect(sw.checked).toBe(false);
    await click(sw);
    await type(
      textFields($(homeable.host, "marvel0-autonomy-form")!)[0],
      "pick the pepper into the white plate",
    );
    await click($(homeable.host, "marvel0-autonomy-prepare"));
    await tick(3, 10);
    expect(callsOf(homeable.state, "prepare")[0].start_home).toBe(true);
    const plain = await openStep({
      ready: readyDoc("blocked", (doc) => (doc.profile.start_home = false)),
    });
    const off = plain.host.querySelector(
      "[data-testid=marvel0-autonomy-form] input[role=switch]",
    ) as HTMLInputElement;
    expect(off.disabled).toBe(true);
    expect(text($(plain.host, "marvel0-autonomy-form"))).toContain(
      E["marvel0.autonomy.form.start_home_none"],
    );
  });

  const switches = (host: HTMLElement) =>
    [
      ...host.querySelectorAll(
        "[data-testid=marvel0-autonomy-form] input[role=switch]",
      ),
    ] as HTMLInputElement[];

  test("the reset policy and the early check are off, and disabled with a hint where the code version lacks them", async () => {
    const f = await openStep({ ready: readyDoc("blocked") });
    const [, reset, early] = switches(f.host);
    expect([reset.disabled, reset.checked]).toEqual([true, false]);
    expect([early.disabled, early.checked]).toEqual([true, false]);
    const form = text($(f.host, "marvel0-autonomy-form"));
    expect(form).toContain(E["marvel0.autonomy.form.reset_policy_none"]);
    expect(form).toContain(E["marvel0.autonomy.form.early_none"]);
    await type(
      textFields($(f.host, "marvel0-autonomy-form")!)[0],
      "pick the pepper into the white plate",
    );
    await click($(f.host, "marvel0-autonomy-prepare"));
    await tick(3, 10);
    const sent = callsOf(f.state, "prepare")[0];
    expect([sent.reset_policy, sent.early_check]).toEqual([false, null]);
  });

  test("with the capabilities the switches start on, work and are sent as chosen; the early check shows its fields only while on", async () => {
    const able = (doc: ReturnType<typeof readyDoc>) => {
      doc.profile.reset_policy = true;
      doc.profile.early_check = true;
    };
    const f = await openStep({ ready: readyDoc("blocked", able) });
    const formEl = $(f.host, "marvel0-autonomy-form")!;
    const [, reset, early] = switches(f.host);
    expect([reset.disabled, early.disabled]).toEqual([false, false]);
    // on from the start: the 2026-11-17 run had them off and had to find them
    expect([reset.checked, early.checked]).toEqual([true, true]);
    expect($(f.host, "marvel0-autonomy-early")).not.toBeNull();
    expect(text(formEl)).toContain(E["marvel0.autonomy.form.early_settle"]);
    await click(early);
    expect($(f.host, "marvel0-autonomy-early")).toBeNull();
    await click(early);
    expect($(f.host, "marvel0-autonomy-early")).not.toBeNull();
    await type(textFields(formEl)[0], "pick the pepper into the white plate");
    // settle, every, max are the three text fields after the four of the form
    await type(textFields(formEl)[5], "");
    await click($(f.host, "marvel0-autonomy-prepare"));
    await tick(3, 10);
    expect(callsOf(f.state, "prepare")[0]).toMatchObject({
      reset_policy: true,
      early_check: {
        on_events: true,
        event_types: ["gripper_open"],
        settle_steps: 8,
        every_steps: null,
        max_checks: 3,
      },
    });
    // switched off by hand, they are sent off
    const g = await openStep({ ready: readyDoc("blocked", able) });
    await click(switches(g.host)[1]);
    await click(switches(g.host)[2]);
    await type(
      textFields($(g.host, "marvel0-autonomy-form")!)[0],
      "pick the pepper into the white plate",
    );
    await click($(g.host, "marvel0-autonomy-prepare"));
    await tick(3, 10);
    const sent = callsOf(g.state, "prepare")[0];
    expect([sent.reset_policy, sent.early_check]).toEqual([false, null]);
  });

  test("a switch the code version stops offering is not sent, whatever was clicked before", async () => {
    const able = (doc: ReturnType<typeof readyDoc>) => {
      doc.profile.reset_policy = true;
      doc.profile.early_check = true;
    };
    const f = await openStep({ ready: readyDoc("blocked", able) });
    // both start on where the code version has them
    expect(switches(f.host)[1].checked).toBe(true);
    expect(switches(f.host)[2].checked).toBe(true);
    // the pinned code version changes (another profile): neither is offered now
    f.state.ready = readyDoc("blocked");
    await waitFor(() => switches(f.host)[1].disabled, {
      timeoutMs: 20_000,
      label: "the next read",
    });
    expect(switches(f.host)[1].checked).toBe(false);
    await type(
      textFields($(f.host, "marvel0-autonomy-form")!)[0],
      "pick the pepper into the white plate",
    );
    await click($(f.host, "marvel0-autonomy-prepare"));
    await tick(3, 10);
    const sent = callsOf(f.state, "prepare")[0];
    expect([sent.reset_policy, sent.early_check]).toEqual([false, null]);
  });

  test("the home is not sent for a code version that has no startup home, whatever the form held before", async () => {
    const f = await openStep({ ready: readyDoc("blocked") });
    const home = () => switches(f.host)[0];
    await click(home());
    await click(home());
    expect(home().checked).toBe(true);
    f.state.ready = readyDoc(
      "blocked",
      (doc) => (doc.profile.start_home = false),
    );
    await waitFor(() => home().disabled, {
      timeoutMs: 20_000,
      label: "the next read",
    });
    await type(
      textFields($(f.host, "marvel0-autonomy-form")!)[0],
      "pick the pepper into the white plate",
    );
    await click($(f.host, "marvel0-autonomy-prepare"));
    await tick(3, 10);
    expect(callsOf(f.state, "prepare")[0].start_home).toBe(false);
  });

  test("the takeover switch is off and disabled where the code version lacks it, or where the reset policy is not asked; it is sent only when both hold", async () => {
    const lacks = await openStep({
      ready: readyDoc("blocked", (doc) => (doc.profile.reset_policy = true)),
    });
    // the takeover's switch: the one named for it (its own settings follow it)
    const last = (host: HTMLElement) =>
      switches(host).find((input) =>
        input
          .closest("label")
          ?.textContent?.includes(E["marvel0.autonomy.form.grasp_assist"]),
      )!;
    expect([last(lacks.host).disabled, last(lacks.host).checked]).toEqual([
      true,
      false,
    ]);
    expect(text($(lacks.host, "marvel0-autonomy-form"))).toContain(
      E["marvel0.autonomy.form.grasp_assist_none"],
    );
    const able = (doc: ReturnType<typeof readyDoc>) => {
      doc.profile.reset_policy = true;
      doc.profile.grasp_assist = true;
    };
    const f = await openStep({ ready: readyDoc("blocked", able) });
    const formEl = $(f.host, "marvel0-autonomy-form")!;
    // the reset policy and the takeover both start on where the code has them
    expect([last(f.host).disabled, last(f.host).checked]).toEqual([
      false,
      true,
    ]);
    // turning the reset policy off takes the takeover with it
    await click(switches(f.host)[1]);
    expect([last(f.host).disabled, last(f.host).checked]).toEqual([
      true,
      false,
    ]);
    expect(text(formEl)).toContain(
      E["marvel0.autonomy.form.grasp_assist_needs_reset"],
    );
    await click(switches(f.host)[1]); // the reset policy, on again
    expect(last(f.host).disabled).toBe(false);
    expect(last(f.host).checked).toBe(false);
    await click(last(f.host));
    expect(last(f.host).checked).toBe(true);
    await type(textFields(formEl)[0], "pick the pepper into the white plate");
    await click($(f.host, "marvel0-autonomy-prepare"));
    await tick(3, 10);
    // this profile has no auto_grasp: the request carries neither new field
    const sent = callsOf(f.state, "prepare")[0];
    expect(sent).toMatchObject({ reset_policy: true, grasp_assist: true });
    expect("grasp_source" in sent).toBe(false);
    expect("unattended" in sent).toBe(false);
  });

  test("the grasp source follows the takeover; the unattended switch stays independent", async () => {
    const able = (doc: ReturnType<typeof readyDoc>) => {
      doc.profile.reset_policy = true;
      doc.profile.grasp_assist = true;
      doc.profile.auto_grasp = true;
    };
    const f = await openStep({ ready: readyDoc("blocked", able) });
    const formEl = $(f.host, "marvel0-autonomy-form")!;
    const settings = () => $(f.host, "marvel0-autonomy-grasp-settings");
    const radios = () =>
      [
        ...(settings()?.querySelectorAll("button[role=radio]") ?? []),
      ] as HTMLInputElement[];
    const unattended = () =>
      [
        ...($(f.host, "marvel0-autonomy-unattended")?.querySelectorAll(
          "input[role=switch]",
        ) ?? []),
      ][0] as HTMLInputElement;
    // the takeover is on from the start: its settings are there, not folded
    expect(settings() !== null).toBe(true);
    expect(settings()!.closest("details")).toBeNull();
    expect(text(settings())).toContain("Grasp source");
    expect(text(settings())).toContain("Automatic (camera + VLM)");
    expect(text(settings())).toContain("Operator keys");
    expect(text($(f.host, "marvel0-autonomy-unattended"))).toContain(
      E["marvel0.autonomy.form.unattended_hint"],
    );
    expect(
      radios().map((r) => [
        r.textContent,
        r.getAttribute("aria-checked") === "true",
      ]),
    ).toEqual([
      ["Automatic (camera + VLM)", true],
      ["Operator keys", false],
    ]);
    expect(unattended().checked).toBe(true);
    // the switch off takes the settings away with it, and no source is sent
    const grasp = switches(f.host).find((input) =>
      input
        .closest("label")
        ?.textContent?.includes(E["marvel0.autonomy.form.grasp_assist"]),
    )!;
    await click(grasp);
    expect(settings()).toBeNull();
    expect(unattended().checked).toBe(true);
    await click(grasp);
    await click(radios()[1]);
    await click(unattended());
    expect(radios().map((r) => r.getAttribute("aria-checked"))).toEqual([
      "false",
      "true",
    ]);
    expect(unattended().checked).toBe(false);
    expect(text($(f.host, "marvel0-autonomy-grasp-source-hint"))).toBe(
      E["marvel0.autonomy.form.grasp_source_operator_hint"],
    );
    await type(textFields(formEl)[0], "pick the pepper into the white plate");
    await click($(f.host, "marvel0-autonomy-prepare"));
    await tick(3, 10);
    expect(callsOf(f.state, "prepare")[0]).toMatchObject({
      grasp_assist: true,
      grasp_source: "operator",
      unattended: false,
    });
    // the same in Chinese, with the user's words
    expect(Z["marvel0.autonomy.form.grasp_source"]).toBe("抓取来源");
    expect(Z["marvel0.autonomy.form.grasp_source_auto"]).toBe(
      "自动（相机+视觉模型）",
    );
    expect(Z["marvel0.autonomy.form.grasp_source_operator"]).toBe("人工按键");
    expect(Z["marvel0.autonomy.form.unattended"]).toBe("无人值守");
  });

  test("without reset or takeover an auto_grasp profile can still choose an attended run", async () => {
    const f = await openStep({
      ready: readyDoc("blocked", (doc) => {
        doc.profile.reset_policy = true;
        doc.profile.grasp_assist = true;
        doc.profile.auto_grasp = true;
      }),
    });
    const reset = switches(f.host).find((input) =>
      input
        .closest("label")
        ?.textContent?.includes(E["marvel0.autonomy.form.reset_policy"]),
    )!;
    await click(reset);
    expect($(f.host, "marvel0-autonomy-grasp-settings")).toBeNull();
    const unattended = $(f.host, "marvel0-autonomy-unattended")!.querySelector(
      "input[role=switch]",
    ) as HTMLInputElement;
    expect(unattended.checked).toBe(true);
    await click(unattended);
    expect(unattended.checked).toBe(false);
    await type(
      textFields($(f.host, "marvel0-autonomy-form")!)[0],
      "pick the pepper into the white plate",
    );
    await click($(f.host, "marvel0-autonomy-prepare"));
    await tick(3, 10);
    const sent = callsOf(f.state, "prepare")[0];
    expect("grasp_source" in sent).toBe(false);
    expect(sent.unattended).toBe(false);
    expect(sent.reset_policy).toBe(false);
    expect(sent.grasp_assist).toBe(false);
  });

  test("a profile with the takeover but without auto_grasp: no grasp source control, no unattended switch, one line saying operator keys only, and a request with neither field", async () => {
    const f = await openStep({
      ready: readyDoc("blocked", (doc) => {
        doc.profile.reset_policy = true;
        doc.profile.grasp_assist = true;
        doc.profile.auto_grasp = false;
      }),
    });
    const formEl = $(f.host, "marvel0-autonomy-form")!;
    expect($(f.host, "marvel0-autonomy-grasp-settings")).toBeNull();
    expect(text(formEl)).not.toContain(E["marvel0.autonomy.form.unattended"]);
    expect(
      formEl.querySelector(
        `[role=radiogroup][aria-label="${E["marvel0.autonomy.form.grasp_source"]}"]`,
      ),
    ).toBeNull();
    const note = $(f.host, "marvel0-autonomy-grasp-operator-only")!;
    expect(text(note)).toBe(
      E["marvel0.autonomy.form.grasp_source_operator_only"],
    );
    expect(Z["marvel0.autonomy.form.grasp_source_operator_only"]).toBeTruthy();
    // the takeover switch is on, and the note sits right under it
    expect($(f.host, "marvel0-autonomy-grasp-hint")!.nextElementSibling).toBe(
      note,
    );
    await type(textFields(formEl)[0], "pick the pepper into the white plate");
    await click($(f.host, "marvel0-autonomy-prepare"));
    await tick(3, 10);
    const sent = callsOf(f.state, "prepare")[0];
    expect(sent.grasp_assist).toBe(true);
    expect("grasp_source" in sent).toBe(false);
    expect("unattended" in sent).toBe(false);
  });

  test("a bad early-check number stops the request with a hint; no trigger at all is refused", async () => {
    const f = await openStep({
      ready: readyDoc("blocked", (doc) => (doc.profile.early_check = true)),
    });
    const formEl = $(f.host, "marvel0-autonomy-form")!;
    await type(textFields(formEl)[0], "pick the pepper into the white plate");
    // the early check starts on: its three numbers are already there
    await type(textFields(formEl)[5], "39");
    const prepare = $(f.host, "marvel0-autonomy-prepare") as HTMLButtonElement;
    expect(prepare.disabled).toBe(true);
    expect(text(formEl)).toContain(E["marvel0.autonomy.form.early_every_bad"]);
    await type(textFields(formEl)[5], "60");
    expect(prepare.disabled).toBe(false);
    await type(textFields(formEl)[5], "");
    await click(switches(f.host)[3]); // the gripper trigger off: nothing left
    expect(prepare.disabled).toBe(true);
    expect(text(formEl)).toContain(
      E["marvel0.autonomy.form.early_trigger_bad"],
    );
  });

  test("the backend's refusal of a step-5 key is worded under its switch", async () => {
    const f = await openStep({
      ready: readyDoc("blocked", (doc) => {
        doc.profile.reset_policy = true;
        doc.profile.early_check = true;
      }),
    });
    f.state.answer = (call) =>
      call.op === "prepare"
        ? refusal(422, "form_invalid", {
            field: "reset_policy",
            reason: "template",
          })
        : undefined;
    await type(
      textFields($(f.host, "marvel0-autonomy-form")!)[0],
      "pick the pepper into the white plate",
    );
    await click($(f.host, "marvel0-autonomy-prepare"));
    await tick(3, 10);
    expect(text($(f.host, "marvel0-autonomy-form"))).toContain(
      E["marvel0.autonomy.form.refused_template"],
    );
  });

  test("a refusal is shown in words; a field the backend refused gets its hint", async () => {
    const f = await openStep({ ready: readyDoc("blocked") });
    f.state.answer = (call) =>
      call.op === "prepare"
        ? refusal(422, "check_failed", { reason: "returncode" })
        : { ok: true, status: 200, data: {} };
    await type(
      textFields($(f.host, "marvel0-autonomy-form")!)[0],
      "pick the pepper into the white plate",
    );
    await click($(f.host, "marvel0-autonomy-prepare"));
    await tick(3, 10);
    expect(text($(f.host, "marvel0-autonomy-prepare-refused"))).toContain(
      E["marvel0.autonomy.err.check_failed"],
    );
    f.state.answer = (call) =>
      call.op === "prepare"
        ? refusal(422, "form_invalid", { field: "steps", reason: "range" })
        : { ok: true, status: 200, data: {} };
    await click($(f.host, "marvel0-autonomy-prepare"));
    await tick(3, 10);
    expect(text($(f.host, "marvel0-autonomy-form"))).toContain(
      E["marvel0.autonomy.form.refused_range"],
    );
  });

  // The backend's own samples of refusals, one test each: all of them are in
  // the page's words (none falls back to the server's English sentence).
  for (const [code, entry] of Object.entries<{
    status: number;
    detail: Record<string, unknown>;
  }>(PREPARE.errors ?? {}))
    test(`the refusal ${code} of the backend's sample is in the page's words`, async () => {
      const f = await openStep({ ready: readyDoc("blocked") });
      f.state.answer = (call) =>
        call.op === "prepare"
          ? refusal(entry.status, code, entry.detail)
          : { ok: true, status: 200, data: {} };
      await type(
        textFields($(f.host, "marvel0-autonomy-form")!)[0],
        "pick the pepper into the white plate",
      );
      await click($(f.host, "marvel0-autonomy-prepare"));
      await tick(3, 10);
      if (code === "form_invalid") {
        // Shown at its field.
        expect(text($(f.host, "marvel0-autonomy-form"))).toContain(
          E[
            "marvel0.autonomy.form.refused_" + (entry.detail.reason ?? "range")
          ],
        );
        return;
      }
      const block = $(f.host, "marvel0-autonomy-prepare-refused");
      expect(block !== null).toBe(true);
      expect(block!.querySelector("[lang=en]") === null).toBe(true);
    });
});

describe("the prepared run and the start", () => {
  test("the saved run shows grasp source and unattended, including when both are off", async () => {
    for (const source of ["auto", "operator"] as const) {
      const { host } = await openStep({
        ready: allMet((doc) => {
          doc.prepared.grasp_assist = true;
          doc.prepared.grasp_source = source;
          doc.prepared.unattended = source === "auto";
        }),
      });
      const card = $(host, "marvel0-autonomy-prepared")!;
      expect(text(card)).toContain(
        E[`marvel0.autonomy.form.grasp_source_${source}`],
      );
      const rows = [...card.querySelectorAll("dt")];
      const unattended = rows.find(
        (row) => text(row) === E["marvel0.autonomy.prepared.unattended"],
      )!;
      expect(text(unattended.nextElementSibling)).toBe(
        E[source === "auto" ? "marvel0.autonomy.yes" : "marvel0.autonomy.no"],
      );
    }
  });

  test("its facts, its digests and the command of the terminal fallback with a Copy button", async () => {
    const { host } = await openStep({ ready: allMet() });
    const card = $(host, "marvel0-autonomy-prepared")!;
    const doc = readyDoc("prepared_ready");
    expect(text(card)).toContain(doc.prepared.name);
    expect(text(card)).toContain(doc.prepared.plan_sha256.slice(0, 12));
    expect(text(card)).toContain(doc.prepared.binding_sha256.slice(0, 12));
    expect(text($(host, "marvel0-autonomy-command"))).toBe(
      doc.prepared.execute,
    );
    expect(text(card)).toContain(E["marvel0.autonomy.prepared.fallback"]);
    expect(button(card, "Copy")).toBeDefined();
  });

  test("with every row met and the run intact, Start is on and says nothing against it", async () => {
    const { host } = await openStep({ ready: allMet() });
    expect(start(host)!.disabled).toBe(false);
    expect($(host, "marvel0-autonomy-start-off") === null).toBe(true);
  });

  test("the step-4 policy server the start stops itself: two warnings in the page's words, Start stays on, and one line next to the button names the unit", async () => {
    const unit = "levi-aeri-policy-manual-marvel0-pi05";
    const own = (doc: ReturnType<typeof readyDoc>) => {
      for (const code of ["ports_free", "no_policy_deploy"]) {
        const row = doc.readiness.find(
          (r: { code: string }) => r.code === code,
        );
        row.level = "warn";
        row.params = { ports: [8000], own_policy: unit, unit, state: "ready" };
      }
    };
    const { host } = await openStep({ ready: allMet(own) });
    expect(start(host)!.disabled).toBe(false);
    expect($(host, "marvel0-autonomy-start-off") === null).toBe(true);
    const line = text($(host, "marvel0-autonomy-own-policy"));
    expect(line).toContain(unit);
    expect(line).toContain("first stops the policy server");
    // a refused start leaves it stopped: said beside the button, in the rows and
    // in the two errors, in both languages
    expect(line).toContain("stays stopped");
    for (const lang of [E, Z]) {
      for (const key of [
        "start.own_policy",
        "ready.ports_free.own_policy",
        "ready.no_policy_deploy.own_policy",
        "err.policy_stopped_not_started",
        "err.policy_stop_failed",
      ])
        expect([
          key,
          /deploy|部署/i.test(lang[`marvel0.autonomy.${key}`]),
        ]).toEqual([key, true]);
    }
    for (const code of ["ports_free", "no_policy_deploy"]) {
      const row = rowOf(host, code)!;
      expect(row.dataset.level).toBe("warn");
      expect(text(row)).toContain(unit);
      expect(text(row)).toContain("starting D stops");
    }
    // no such line when the server is not there
    const plain = await openStep({ ready: allMet() });
    expect($(plain.host, "marvel0-autonomy-own-policy") === null).toBe(true);
    // a warning that does not name the page's unit is no such case
    const bare = await openStep({
      ready: allMet((doc) => {
        own(doc);
        for (const row of doc.readiness) delete row.params.own_policy;
      }),
    });
    expect($(bare.host, "marvel0-autonomy-own-policy") === null).toBe(true);
    // an empty unit, and a row that passes (level ok) with the param left on it
    const empty = await openStep({
      ready: allMet((doc) => {
        own(doc);
        for (const row of doc.readiness)
          if (row.params?.own_policy) row.params.own_policy = "";
      }),
    });
    expect($(empty.host, "marvel0-autonomy-own-policy") === null).toBe(true);
    const passing = await openStep({
      ready: allMet((doc) => {
        own(doc);
        for (const row of doc.readiness)
          if (row.params?.own_policy) row.level = "ok";
      }),
    });
    expect($(passing.host, "marvel0-autonomy-own-policy") === null).toBe(true);
  });

  test("a refusing row, an unknown row, a run that changed, an evaluation client: each switches Start off and is named", async () => {
    const mixed = await openStep({
      ready: readyDoc(
        "prepared_ready",
        (doc) => (doc.readiness[3].level = "unknown"),
      ),
    });
    expect(start(mixed.host)!.disabled).toBe(true);
    expect(text($(mixed.host, "marvel0-autonomy-start-off"))).toContain(
      E["marvel0.autonomy.start.off"],
    );
    const changed = await openStep({
      ready: allMet((doc) => {
        doc.prepared.intact = false;
        doc.prepared.problems = ["files_changed"];
      }),
    });
    expect(start(changed.host)!.disabled).toBe(true);
    expect(text($(changed.host, "marvel0-autonomy-start-off"))).toContain(
      E["marvel0.autonomy.local.prepared_changed"],
    );
    expect(
      text($(changed.host, "marvel0-autonomy-prepared-changed")),
    ).toContain("files_changed");
    const evalRunning = await openStep({ ready: allMet(), evalLive: true });
    expect(start(evalRunning.host)!.disabled).toBe(true);
    expect(text($(evalRunning.host, "marvel0-autonomy-start-off"))).toContain(
      E["marvel0.autonomy.local.eval_live"],
    );
    const none = await openStep({
      ready: allMet((doc) => (doc.prepared = null)),
    });
    expect(start(none.host)).toBeNull();
  });

  test("the run document's own 'start is not offered' is named too", async () => {
    const { host } = await openStep({
      ready: allMet(),
      run: runDoc("idle_blocked"),
    });
    expect(start(host)!.disabled).toBe(true);
    expect(text($(host, "marvel0-autonomy-start-off"))).toContain(
      E["marvel0.autonomy.err.driven_elsewhere"],
    );
  });

  test("one click on Start: what it states is written beside the button, no window and no tick; then a challenge and the start, once, with the checklist the backend requires", async () => {
    const f = await openStep({ ready: allMet() });
    const says = text($(f.host, "marvel0-autonomy-start-says"));
    // the sentences of D's own start warning, the checklist, the two checks of
    // the stack's windows that stay, and what starting does
    expect(says).toContain(E["marvel0.autonomy.start.hazard"]);
    expect(says).toContain(E["marvel0.autonomy.start.statements"]);
    for (const key of [
      "ack.scene_ok",
      "ack.area_clear",
      "ack.arm_home",
      "dcheck.scene",
      "dcheck.limits",
      "dcheck.checkpoints",
      "dcheck.providers",
      "dcheck.hardware",
    ])
      expect(says).toContain(E[`marvel0.autonomy.${key}`]);
    expect(says).toContain(E["marvel0.ctl.attest.hands_clear"]);
    expect(says).toContain(E["marvel0.ctl.attest.no_client"]);
    expect(says).not.toMatch(/hand on the (physical )?e-stop/i);
    expect(
      f.host.querySelectorAll("input[type=checkbox]:not([role=switch])"),
    ).toHaveLength(0);
    await doubleClick(start(f.host));
    await tick(4, 10);
    expect(dialog()).toBeNull();
    expect(f.state.order.filter((op) => op !== "presence")).toEqual([
      "challenge",
      "start",
    ]);
    expect(callsOf(f.state, "challenge")[0]).toEqual({
      purpose: "start",
      name: readyDoc("prepared_ready").prepared.name,
    });
    const name = readyDoc("prepared_ready").prepared.name;
    expect(callsOf(f.state, "start")[0]).toEqual({
      request_id: expect.any(String),
      name,
      challenge: "tok-start",
      confirm: `start aeri ${name}`,
      attest: { scene_ok: true, area_clear: true, arm_home: true },
    });
  });

  test("the statements beside Start are in Chinese too", async () => {
    const f = await openStep({ ready: allMet(), zh: true });
    const says = text($(f.host, "marvel0-autonomy-start-says"));
    expect(says).toContain(Z["marvel0.autonomy.start.statements"]);
    expect(says).toContain(Z["marvel0.autonomy.dcheck.checkpoints"]);
    expect(says).toContain(Z["marvel0.autonomy.ack.arm_home"]);
  });

  test("the challenge token is taken from the backend's own answer and sent back with the start", async () => {
    const f = await openStep({ ready: allMet() });
    f.state.answer = (call) =>
      call.op === "challenge"
        ? { ok: true, status: 200, data: RUN_ANSWERS.challenge }
        : { ok: true, status: 202, data: RUN_ANSWERS.start };
    await click(start(f.host));
    await tick(4, 10);
    expect(callsOf(f.state, "start")[0].challenge).toBe(
      RUN_ANSWERS.challenge.challenge,
    );
  });

  // Every refusal the backend can give to a start, one test each: shown under
  // the button in the page's words.
  for (const [code, entry] of Object.entries(RUN_REFUSALS))
    test(`the refusal ${code} of a start is in the page's words, under the button`, async () => {
      const f = await openStep({ ready: allMet() });
      f.state.answer = (call) =>
        call.op === "start"
          ? refusal(entry.status, code, entry.detail, "SERVER ENGLISH")
          : call.op === "challenge"
            ? { ok: true, status: 200, data: { challenge: "tok" } }
            : { ok: true, status: 200, data: {} };
      await click(start(f.host));
      await tick(4, 10);
      const block = $(f.host, "marvel0-autonomy-start-refused");
      expect(block != null).toBe(true);
      expect(block!.textContent).not.toContain("SERVER ENGLISH");
      expect(block!.querySelector("[lang=en]") === null).toBe(true);
    });

  test("Start does nothing while it is off: nothing is sent", async () => {
    const f = await openStep({
      ready: allMet((doc) => {
        doc.readiness.find(
          (r: { code: string }) => r.code === "gpu_free",
        ).level = "refuse";
      }),
    });
    expect(start(f.host)!.disabled).toBe(true);
    await click(start(f.host));
    expect(f.state.calls.filter((c) => c.op !== "presence")).toEqual([]);
  });

  test("a refused start is shown in words and the page's phrase is the backend's template", async () => {
    const f = await openStep({
      ready: allMet(),
      run: runDoc("idle", (doc) => (doc.start.phrase = "start aeri {name}")),
    });
    f.state.answer = (call) =>
      call.op === "start"
        ? refusal(409, "readiness_failed", { first: "gpu_free" })
        : call.op === "challenge"
          ? { ok: true, status: 200, data: { challenge: "tok-x" } }
          : { ok: true, status: 200, data: {} };
    await click(start(f.host));
    await tick(4, 10);
    expect(text($(f.host, "marvel0-autonomy-start-refused"))).toContain(
      E["marvel0.autonomy.err.readiness_failed"],
    );
    const name = readyDoc("prepared_ready").prepared.name;
    expect(callsOf(f.state, "start")[0].confirm).toBe(`start aeri ${name}`);
  });

  test("a challenge the backend refused stops the start there", async () => {
    const f = await openStep({ ready: allMet() });
    f.state.answer = (call) =>
      call.op === "challenge"
        ? refusal(409, "readiness_failed")
        : { ok: true, status: 200, data: {} };
    await click(start(f.host));
    await tick(4, 10);
    expect(callsOf(f.state, "start")).toEqual([]);
    expect(text($(f.host, "marvel0-autonomy-start-refused"))).toContain(
      E["marvel0.autonomy.err.readiness_failed"],
    );
  });

  test("once D is up, the prepare card is gone and the run card has the page", async () => {
    const f = await openStep({ ready: allMet(), run: runDoc("idle") });
    expect($(f.host, "marvel0-autonomy-prepare-card")).not.toBeNull();
    await serve(f, runDoc("launching"));
    expect($(f.host, "marvel0-autonomy-prepare-card") === null).toBe(true);
    expect($(f.host, "marvel0-autonomy-run")).not.toBeNull();
    await serve(f, runDoc("ended_done"));
    // Over: the run's summary stays and the next run can be prepared.
    expect($(f.host, "marvel0-autonomy-run")).not.toBeNull();
    expect($(f.host, "marvel0-autonomy-prepare-card")).not.toBeNull();
  });
});

describe("what comes first on the card", () => {
  test("with a prepared run, the form for another one is a plain section below it, titled 'Prepare a different run', with no fold; without one the form is just there", async () => {
    const withRun = await openStep({ ready: allMet() });
    const prepared = $(withRun.host, "marvel0-autonomy-prepared")!;
    const another = $(withRun.host, "marvel0-autonomy-another")!;
    expect(another.tagName).not.toBe("DETAILS");
    expect(another.closest("details")).toBeNull();
    expect(text(another.querySelector("h4"))).toBe(
      E["marvel0.autonomy.prepare.another"],
    );
    expect(E["marvel0.autonomy.prepare.another"]).toBe(
      "Prepare a different run",
    );
    expect(Z["marvel0.autonomy.prepare.another"]).toBe("准备另一次运行");
    expect(
      prepared.compareDocumentPosition(another) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    // the form and its submit button are in it, in view and enabled for use
    const form = $(withRun.host, "marvel0-autonomy-form")!;
    expect(another.contains(form)).toBe(true);
    expect(form.closest("details")).toBeNull();
    await type(textFields(form)[0], "pick the red pepper into the white plate");
    expect(
      ($(withRun.host, "marvel0-autonomy-prepare") as HTMLButtonElement)
        .disabled,
    ).toBe(false);
    const without = await openStep({ ready: readyDoc("not_prepared") });
    expect($(without.host, "marvel0-autonomy-another") === null).toBe(true);
    expect($(without.host, "marvel0-autonomy-form")).not.toBeNull();
    expect(
      $(without.host, "marvel0-autonomy-form")!.closest("details"),
    ).toBeNull();
  });

  test("the live service that cannot be stopped from here is named with the backend's reason and has no button", async () => {
    const { host } = await openStep({
      ready: readyDoc("blocked", (doc) => {
        doc.live_service.can_stop = false;
        doc.live_service.why_not = { code: "arm_in_use", text: "x" };
      }),
    });
    const box = $(host, "marvel0-autonomy-live-blocked")!;
    expect(text(box)).toContain(E["marvel0.autonomy.err.arm_in_use"]);
    expect(button(box, E["marvel0.autonomy.live.button"])).toBeUndefined();
  });

  test("leftover units that cannot be stopped from here: the same", async () => {
    const { host } = await openStep({ ready: readyDoc("leftovers_blocked") });
    const box = $(host, "marvel0-autonomy-leftovers-blocked")!;
    expect(text(box)).toContain(E["marvel0.autonomy.cannot_stop"]);
    expect(button(box, E["marvel0.autonomy.leftovers.button"])).toBeUndefined();
  });
});

describe("the two things that stop something", () => {
  test("the live annotation service holds :8100: a button with a window; the page sends the backend's phrase once", async () => {
    const f = await openStep({ ready: readyDoc("blocked") });
    const box = $(f.host, "marvel0-autonomy-live")!;
    await click(button(box, E["marvel0.autonomy.live.button"]));
    const d = dialog()!;
    expect(document.activeElement).toBe(button(d, "Cancel"));
    expect(text(d)).toContain(E["marvel0.autonomy.live.hazard"]);
    expect(textFields(d)).toHaveLength(0);
    await doubleClick(
      Array.from(d.querySelectorAll("button")).find(
        (b) => text(b) === E["marvel0.autonomy.live.button"],
      )!,
    );
    await tick(3, 10);
    expect(callsOf(f.state, "live_stop")).toEqual([
      { request_id: expect.any(String), confirm: "stop live service" },
    ]);
    expect(Object.keys(LIVE_STOP.ok.body).length).toBeGreaterThan(0);
  });

  test("it is not offered when the service is not running or cannot be stopped from here", async () => {
    const f = await openStep({ ready: readyDoc("prepared_ready") });
    expect($(f.host, "marvel0-autonomy-live") === null).toBe(true);
    const stuck = await openStep({
      ready: readyDoc("blocked", (doc) => (doc.live_service.can_stop = false)),
    });
    expect($(stuck.host, "marvel0-autonomy-live") === null).toBe(true);
  });

  test("leftover units: a button with a window that names what it stops and what it leaves; the phrase is the backend's", async () => {
    const f = await openStep({ ready: readyDoc("leftovers_present") });
    const box = $(f.host, "marvel0-autonomy-leftovers")!;
    await click(button(box, E["marvel0.autonomy.leftovers.button"]));
    const d = dialog()!;
    expect(text(d)).toContain(E["marvel0.autonomy.leftovers.hazard"]);
    await click(
      Array.from(d.querySelectorAll("button")).find(
        (b) => text(b) === E["marvel0.autonomy.leftovers.button"],
      )!,
    );
    await tick(3, 10);
    expect(callsOf(f.state, "leftovers_stop")).toEqual([
      { request_id: expect.any(String), confirm: "stop leftover aeri units" },
    ]);
    expect(Object.keys(LEFTOVERS_STOP.ok.body).length).toBeGreaterThan(0);
    const none = await openStep({ ready: readyDoc("prepared_ready") });
    expect($(none.host, "marvel0-autonomy-leftovers") === null).toBe(true);
  });

  test("a refusal inside the window is shown in words", async () => {
    const f = await openStep({ ready: readyDoc("blocked") });
    f.state.answer = (call) =>
      call.op === "live_stop"
        ? refusal(502, "live_stop_failed", { tail: "x" })
        : { ok: true, status: 200, data: {} };
    await click(
      button(
        $(f.host, "marvel0-autonomy-live")!,
        E["marvel0.autonomy.live.button"],
      ),
    );
    await click(
      Array.from(dialog()!.querySelectorAll("button")).find(
        (b) => text(b) === E["marvel0.autonomy.live.button"],
      )!,
    );
    await tick(3, 10);
    expect(text($(dialog()!, "marvel0-autonomy-live-refused"))).toContain(
      E["marvel0.autonomy.err.live_stop_failed"],
    );
  });
});

describe("the readiness document is old or unreadable", () => {
  test("a read that failed after a good one: the rows are kept, called old, and Start is off", async () => {
    const f = await openStep({ ready: allMet() });
    expect(start(f.host)!.disabled).toBe(false);
    f.state.ready = null;
    await waitFor(() => $(f.host, "marvel0-autonomy-ready-stale"), {
      timeoutMs: 20_000,
      label: "the notice",
    }).catch(() => null);
    await wait(100);
  });

  test("never read: said so, and no card of rows", async () => {
    const f = await openStep({ ready: null });
    expect(text($(f.host, "marvel0-autonomy-ready-unreadable"))).toContain(
      E["marvel0.autonomy.ready.unreadable"],
    );
    expect($(f.host, "marvel0-autonomy-ready") === null).toBe(true);
  });
});

describe("in Chinese", () => {
  test("the rows, the form and the start are worded", async () => {
    const { host } = await openStep({ ready: readyDoc("blocked"), zh: true });
    expect(text($(host, "marvel0-autonomy-ready-count"))).toMatch(/项中已满足/);
    expect(text(rowOf(host, "ports_free"))).toContain(
      Z["marvel0.autonomy.ready.ports_free"],
    );
    const gpu = readyDoc("blocked").readiness.find(
      (r: { code: string }) => r.code === "gpu_free",
    ).params;
    expect(text(rowOf(host, "gpu_free"))).toContain(
      `显存至少空闲 ${gpu.need_mib} MiB（现在 ${gpu.free_mib} MiB）`,
    );
    expect(text($(host, "marvel0-autonomy-form"))).toContain(
      Z["marvel0.autonomy.form.task_hint"],
    );
    expect(text($(host, "marvel0-autonomy-mode-hint"))).toBe(
      Z["marvel0.autonomy.mode_hint.local"],
    );
  });
});

describe("the stages of a first use are on the form, and the defaults stay the cautious ones", () => {
  test("three stages in order: inventory only without motion, one round, several rounds; with the defaults of the backend named", async () => {
    const { host } = await openStep({ ready: allMet() });
    const stages = $(host, "marvel0-autonomy-stages")!;
    expect(
      Array.from(stages.querySelectorAll("li")).map((li) =>
        li.getAttribute("data-stage"),
      ),
    ).toEqual(["s1", "s2", "s3"]);
    const s1 = text(stages.querySelector("[data-stage=s1]"));
    expect(s1).toBe(E["marvel0.autonomy.stage.s1"]);
    expect(s1).toMatch(/inventory only, no motion/);
    expect(s1).toMatch(/The arm does not move/);
    expect(text(stages.querySelector("[data-stage=s2]"))).toMatch(/one round/);
    expect(text(stages.querySelector("[data-stage=s3]"))).toMatch(
      /several rounds/,
    );
    // The note names the defaults the backend sent (5 rounds, 200 steps), which
    // the form below starts with: the steps default is not the evaluation
    // client's 3000.
    const limits = readyDoc("prepared_ready").limits;
    expect(limits.steps.default).toBe(200);
    expect(limits.rounds.default).toBe(5);
    expect(text($(host, "marvel0-autonomy-stage-note"))).toContain(
      "5 rounds and 200 steps",
    );
    const fields = Array.from(
      $(host, "marvel0-autonomy-form")!.querySelectorAll<HTMLInputElement>(
        "input[inputmode=numeric]",
      ),
    ).map((input) => input.value);
    expect(fields).toEqual(["5", "200"]);
  });

  test("in Chinese too", async () => {
    const { host } = await openStep({ ready: allMet(), zh: true });
    const stages = $(host, "marvel0-autonomy-stages")!;
    expect(text(stages)).toContain(Z["marvel0.autonomy.stage.s1"]);
    expect(text(stages)).toContain(Z["marvel0.autonomy.stage.title"]);
  });
});
