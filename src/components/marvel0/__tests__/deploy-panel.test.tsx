import {
  click,
  render,
  setupDom,
  waitFor,
} from "@/components/ds/__tests__/dom";
import { act } from "react";
import { afterEach, describe, expect, test } from "bun:test";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import { LocaleProvider } from "@/components/levi-locale";
import { Marvel0Actions } from "../marvel0-actions";
import { modelsAnswer } from "./catalog-fixtures";
import { cfgModel, model } from "./catalog-fixtures";
import {
  UNIT,
  checks,
  deployInfo,
  deployed,
  idle,
  ready,
  starting,
  stopped,
} from "./deploy-fixtures";
import {
  button,
  choose,
  fakeApi,
  field,
  nothing,
  ok,
  refused,
  tick,
  type,
  type Canned,
} from "./actions-helpers";

setupDom();
afterEach(() => {
  try {
    localStorage.clear();
  } catch {
    // no storage in this DOM
  }
  delete (document as unknown as Record<string, unknown>).visibilityState;
});

const SFT = "pi05_fr3_all_step49999";
const CFG = "recap_cfg_r2_best_step14300_jax";
// The button names what it stops: nothing on this page reads as an emergency stop.
const STOP = en["marvel0.deploy.btn.stop"];

async function open(
  canned: Canned = {},
  pollMs = 5000,
  zhLanguage = false,
  settled = true,
) {
  if (zhLanguage) localStorage.setItem("levi-language", "zh");
  const { api, calls } = fakeApi(canned);
  const view = await render(
    zhLanguage ? (
      <LocaleProvider>
        <Marvel0Actions api={api} pollMs={pollMs} />
      </LocaleProvider>
    ) : (
      <Marvel0Actions api={api} pollMs={pollMs} />
    ),
  );
  if (settled) {
    await waitFor(
      () => view.host.querySelector("[data-testid=marvel0-deploy]"),
      {
        label: "the policy server card",
      },
    );
    await waitFor(() => view.host.querySelector("[data-state]"), {
      label: "its state",
    });
  }
  return { ...view, api, calls };
}
const card = (host: HTMLElement) =>
  host.querySelector("[data-testid=marvel0-deploy]") as HTMLElement;
const dialog = (host: HTMLElement) =>
  host.querySelector("[role=dialog]") as HTMLElement | null;
const text = (node: Element | null) => node?.textContent ?? "";

describe("the Policy server card", () => {
  test("says what it is: it starts and stops one unit (the page's one e-stop line is at the top, not on every card)", async () => {
    const { host } = await open();
    const body = text(card(host));
    expect(body).toContain(en["marvel0.deploy.scope"]);
    expect(en["marvel0.deploy.scope"]).toContain(
      "only the policy server unit this page created",
    );
    expect(en["marvel0.deploy.scope"]).not.toMatch(/e-?stop|emergency/i);
    // No control is called an e-stop.
    for (const node of host.querySelectorAll("button, summary, [role=button]"))
      expect(node.textContent).not.toMatch(/e-?stop|emergency/i);
  });

  test("an idle marvel0: the state in words, a model list, RTC on, 0.22, and the memory of each fraction", async () => {
    const { host } = await open();
    const c = card(host);
    expect(c.getAttribute("data-state")).toBe("idle");
    expect(text(c)).toContain("Idle");
    const models = field<HTMLSelectElement>(c, "Model")!;
    expect(Array.from(models.options).map((o) => o.textContent)).toEqual([
      `${SFT} · pi05_fr3_all_state`,
      `${CFG} · pi05_fr3_all_state_cfg · unverified`,
    ]);
    expect(models.value).toBe(SFT);
    const rtc = field(c, "RTC (real-time chunking)")!;
    expect(rtc.getAttribute("role")).toBe("switch");
    expect((rtc as HTMLInputElement).checked).toBe(true);
    const fraction = field<HTMLSelectElement>(c, "VRAM fraction")!;
    expect(Array.from(fraction.options).map((o) => o.value)).toEqual([
      "0.22",
      "0.3",
      "0.45",
    ]);
    expect(fraction.value).toBe("0.22");
    expect(text(c)).toContain(
      "needs 8198 MiB free (7174 + 1024); 23507 MiB are free",
    );
    expect(text(c)).toContain(
      "needs 10807 MiB free (9783 + 1024); 23507 MiB are free",
    );
    expect(text(c)).toContain(
      "needs 15698 MiB free (14674 + 1024); 23507 MiB are free",
    );
    expect(button(c, "Deploy")!.disabled).toBe(false);
    // Nothing to stop.
    expect(button(c, STOP)).toBeUndefined();
  });

  test("a model with no recorded real-GPU check carries the unverified label", async () => {
    const { host } = await open();
    const c = card(host);
    expect(text(c)).not.toContain("No real-GPU serving check is recorded");
    await choose(field<HTMLSelectElement>(c, "Model"), CFG);
    expect(text(c)).toContain(
      "No real-GPU serving check is recorded for this model.",
    );
    expect(c.querySelector(".ds-badge")).not.toBeNull();
    expect(text(c)).toContain("unverified");
    expect(text(c)).toContain("Role: forward policy");
    // A guidance model shows its scale.
    expect(field(c, "Guidance scale")).not.toBeNull();
  });

  test("the checkpoints that are not offered are listed with the reason", async () => {
    const { host } = await open();
    const c = card(host);
    expect(text(c)).toContain("2 checkpoint(s) not offered");
    expect(text(c)).toContain("reset_r1_step20000");
    expect(text(c)).toContain("excluded by the site file");
    expect(text(c)).toContain("recap_global_step_10000");
    expect(text(c)).toContain("not deployable");
  });

  test("with no model to deploy it says so and Deploy is off", async () => {
    const { host } = await open({ models: ok(modelsAnswer({ models: [] })) });
    const c = card(host);
    expect(text(c)).toContain(en["marvel0.deploy.form.no_models"]);
    expect(button(c, "Deploy")!.disabled).toBe(true);
  });
});

describe("deploying", () => {
  test("Deploy asks for the typed phrase, then sends one request with exactly the choices", async () => {
    const { host, calls } = await open({
      startDeploy: ok(deployed(), 202),
    });
    await click(button(card(host), "Deploy")!);
    const d = dialog(host)!;
    expect(d).not.toBeNull();
    expect(text(d)).toContain(`Deploy ${SFT}`);
    // It says what it starts, and where the rest is started from.
    expect(text(d)).toContain("It starts nothing else");
    expect(text(d)).toContain("started from their own cards");
    expect(text(d)).toContain("Nothing on this page is an emergency stop");
    expect(text(d)).toContain(`deploy ${SFT}`);
    const confirm = button(d, "Deploy")!;
    expect(confirm.disabled).toBe(true);
    await type(field(d, "Confirmation phrase"), `deploy ${SFT} `);
    expect(button(d, "Deploy")!.disabled).toBe(true);
    await type(field(d, "Confirmation phrase"), `deploy ${SFT}`);
    expect(button(d, "Deploy")!.disabled).toBe(false);
    expect(calls.startDeploy).toHaveLength(0);
    await click(button(d, "Deploy")!);
    await tick();
    expect(calls.startDeploy).toHaveLength(1);
    const body = calls.startDeploy[0];
    expect(body.request_id).toMatch(/^[A-Za-z0-9._:-]{8,80}$/);
    expect(body).toEqual({
      request_id: body.request_id,
      model_id: SFT,
      rtc: true,
      fraction: 0.22,
      cfg_guidance_scale: null,
      accept_unverified: false,
      confirm: `deploy ${SFT}`,
    });
    // The dialog is gone and the answer is in words.
    expect(dialog(host)).toBeNull();
    expect(
      text(host.querySelector("[data-testid=marvel0-deploy-result]")),
    ).toContain(
      "Deploy accepted: the policy server is starting. It needs 8198 MiB free; 23100 MiB were free at the final check.",
    );
  });

  // A server that python has started by hand but that is still loading is not
  // yet in the socket table: no check of the page can see it, so the dialog
  // asks the person not to start one at the same time.
  test("the dialog tells the person not to start a server by hand at the same time", async () => {
    const { host } = await open({ startDeploy: ok(deployed(), 202) });
    await click(button(card(host), "Deploy")!);
    expect(text(dialog(host)!)).toContain(
      "Do not press Deploy while a server is being started by hand in a terminal; the page can only see it once it listens.",
    );
  });

  test("the same warning is shown in Chinese", async () => {
    const { host } = await open(
      { startDeploy: ok(deployed(), 202) },
      5000,
      true,
    );
    await waitFor(() => text(card(host)).includes(zh["marvel0.deploy.title"]), {
      label: "Chinese",
    });
    await click(button(card(host), zh["marvel0.deploy.btn.deploy"])!);
    expect(text(dialog(host)!)).toContain(
      "不要在终端里手动启动策略服务的同时点“部署”；页面要等它开始监听才看得到它。",
    );
  });

  test("the state is read again at once after a deploy", async () => {
    const { host, calls } = await open({ startDeploy: ok(deployed(), 202) });
    const before = calls.deploy;
    await click(button(card(host), "Deploy")!);
    await type(field(dialog(host)!, "Confirmation phrase"), `deploy ${SFT}`);
    await click(button(dialog(host)!, "Deploy")!);
    await tick();
    expect(calls.deploy).toBeGreaterThan(before);
  });

  test("RTC off and another fraction are what is sent", async () => {
    const { host, calls } = await open({ startDeploy: ok(deployed(), 202) });
    const c = card(host);
    await click(field(c, "RTC (real-time chunking)")!);
    await choose(field<HTMLSelectElement>(c, "VRAM fraction"), "0.3");
    await click(button(c, "Deploy")!);
    expect(text(dialog(host))).toContain("RTC off");
    expect(text(dialog(host))).toContain("0.30");
    await type(field(dialog(host)!, "Confirmation phrase"), `deploy ${SFT}`);
    await click(button(dialog(host)!, "Deploy")!);
    await tick();
    expect(calls.startDeploy[0]).toMatchObject({ rtc: false, fraction: 0.3 });
  });

  test("a model without a recorded real-GPU check needs the person to accept that", async () => {
    const { host, calls } = await open({ startDeploy: ok(deployed(), 202) });
    const c = card(host);
    await choose(field<HTMLSelectElement>(c, "Model"), CFG);
    await type(field(c, "Guidance scale"), "1.2");
    await click(button(c, "Deploy")!);
    const d = dialog(host)!;
    await type(field(d, "Confirmation phrase"), `deploy ${CFG}`);
    expect(button(d, "Deploy")!.disabled).toBe(true);
    await click(
      field(d, "I accept deploying a model without a recorded real-GPU check")!,
    );
    expect(button(d, "Deploy")!.disabled).toBe(false);
    await click(button(d, "Deploy")!);
    await tick();
    expect(calls.startDeploy[0]).toMatchObject({
      model_id: CFG,
      cfg_guidance_scale: 1.2,
      accept_unverified: true,
      confirm: `deploy ${CFG}`,
    });
  });

  test("Cancel closes the dialog and sends nothing", async () => {
    const { host, calls } = await open();
    await click(button(card(host), "Deploy")!);
    await type(field(dialog(host)!, "Confirmation phrase"), `deploy ${SFT}`);
    await click(button(dialog(host)!, "Cancel")!);
    expect(dialog(host)).toBeNull();
    expect(calls.startDeploy).toHaveLength(0);
    // The phrase is not kept for the next time.
    await click(button(card(host), "Deploy")!);
    expect(
      (field(dialog(host)!, "Confirmation phrase") as HTMLInputElement).value,
    ).toBe("");
  });

  test("a fraction the card cannot hold keeps Deploy off and says by how much", async () => {
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
          { fraction: 0.22, required_mib: 8198, enough: false },
          { fraction: 0.3, required_mib: 10807, enough: false },
          { fraction: 0.45, required_mib: 15698, enough: false },
        ],
        hint: "x",
      },
    });
    const { host } = await open({ deploy: ok(small) });
    const c = card(host);
    expect(button(c, "Deploy")!.disabled).toBe(true);
    expect(text(c)).toContain(
      "needs 8198 MiB free (7174 + 1024), but only 5100 MiB are free",
    );
    expect(text(c)).toContain("Needs the live vLLM asleep or stopped");
    expect(text(c)).toContain("Deploy is off: at fraction 0.22");
  });

  test("a server that refuses is shown in the page's words, and the person may press again", async () => {
    const { host, calls } = await open({
      startDeploy: refused(409, "port_taken", {
        port: 8000,
        listeners: 1,
        ours: false,
      }),
    });
    await click(button(card(host), "Deploy")!);
    await type(field(dialog(host)!, "Confirmation phrase"), `deploy ${SFT}`);
    await click(button(dialog(host)!, "Deploy")!);
    await tick();
    const problem = host.querySelector("[data-testid=marvel0-deploy-problem]")!;
    expect(text(problem)).toContain("Something already listens on port 8000");
    expect(text(problem)).not.toContain("The server's English sentence");
    expect(dialog(host)).toBeNull();
    expect(button(card(host), "Deploy")!.disabled).toBe(false);
    // A refusal is final for its request id: the next press is a new request.
    await click(button(card(host), "Deploy")!);
    await type(field(dialog(host)!, "Confirmation phrase"), `deploy ${SFT}`);
    await click(button(dialog(host)!, "Deploy")!);
    await tick();
    expect(calls.startDeploy).toHaveLength(2);
    expect(calls.startDeploy[1].request_id).not.toBe(
      calls.startDeploy[0].request_id,
    );
  });

  test("with no answer the same request id is sent again, and a new one once it was answered", async () => {
    let answer = nothing() as
      | ReturnType<typeof ok>
      | ReturnType<typeof nothing>;
    const { host, calls } = await open({ startDeploy: () => answer });
    const press = async () => {
      await click(button(card(host), "Deploy")!);
      await type(field(dialog(host)!, "Confirmation phrase"), `deploy ${SFT}`);
      await click(button(dialog(host)!, "Deploy")!);
      await tick();
    };
    await press();
    expect(
      text(host.querySelector("[data-testid=marvel0-deploy-problem]")),
    ).toContain("not known whether the request arrived");
    await press();
    answer = ok(deployed(), 202);
    await press();
    await press();
    const ids = calls.startDeploy.map((b) => b.request_id);
    expect(ids[1]).toBe(ids[0]);
    expect(ids[2]).toBe(ids[0]);
    expect(ids[3]).not.toBe(ids[0]);
  });

  test("a press cannot be repeated while the request is open", async () => {
    let release: (value: ReturnType<typeof ok>) => void = () => {};
    const pending = new Promise<ReturnType<typeof ok>>((resolve) => {
      release = resolve;
    });
    const { host, calls } = await open({ startDeploy: () => pending });
    await click(button(card(host), "Deploy")!);
    await type(field(dialog(host)!, "Confirmation phrase"), `deploy ${SFT}`);
    await click(button(dialog(host)!, "Deploy")!);
    await click(button(dialog(host)!, "Deploy")!);
    expect(calls.startDeploy).toHaveLength(1);
    expect(button(card(host), "Deploy")!.disabled).toBe(true);
    await act(async () => release(ok(deployed(), 202)));
    await tick();
    expect(calls.startDeploy).toHaveLength(1);
  });

  test("two presses in the same instant send one request", async () => {
    const { host, calls } = await open({ startDeploy: ok(deployed(), 202) });
    await click(button(card(host), "Deploy")!);
    await type(field(dialog(host)!, "Confirmation phrase"), `deploy ${SFT}`);
    const confirm = button(dialog(host)!, "Deploy")!;
    await act(async () => {
      confirm.click();
      confirm.click();
    });
    await tick();
    expect(calls.startDeploy).toHaveLength(1);
  });

  test("the answer stays until the person closes it or presses again", async () => {
    const { host } = await open({ startDeploy: ok(deployed(), 202) });
    const press = async () => {
      await click(button(card(host), "Deploy")!);
      await type(field(dialog(host)!, "Confirmation phrase"), `deploy ${SFT}`);
      await click(button(dialog(host)!, "Deploy")!);
      await tick();
    };
    await press();
    const result = () =>
      host.querySelector("[data-testid=marvel0-deploy-result]");
    expect(result()).not.toBeNull();
    await click(button(result() as HTMLElement, "Close")!);
    expect(result()).toBeNull();
    await press();
    expect(result()).not.toBeNull();
  });

  test("a request answered as already accepted says nothing was started again", async () => {
    const { host } = await open({
      startDeploy: ok(deployed({ replayed: true }), 202),
    });
    await click(button(card(host), "Deploy")!);
    await type(field(dialog(host)!, "Confirmation phrase"), `deploy ${SFT}`);
    await click(button(dialog(host)!, "Deploy")!);
    await tick();
    expect(
      text(host.querySelector("[data-testid=marvel0-deploy-result]")),
    ).toContain("nothing was started again");
  });
});

describe("a server that runs", () => {
  test("starting: what the log has said so far, the memory it holds, Stop; no Deploy", async () => {
    const { host } = await open({ deploy: ok(starting()) });
    const c = card(host);
    expect(c.getAttribute("data-state")).toBe("starting");
    expect(text(c)).toContain("The policy server is starting");
    const rows = Array.from(
      c.querySelectorAll("[data-testid=marvel0-deploy-ready] li"),
    ).map((li) => text(li));
    expect(rows).toHaveLength(4);
    expect(rows[0]).toContain(
      "Norm stats shipped with the checkpoint are loaded",
    );
    expect(rows[0]).toContain("yes");
    expect(rows[1]).toContain("RTC is enabled");
    expect(rows[1]).toContain("not yet");
    expect(text(c)).toContain(
      "The policy server holds 7174 MiB of GPU memory, so live annotation cannot start.",
    );
    expect(button(c, STOP)).toBeDefined();
    expect(button(c, "Deploy")!.disabled).toBe(true);
    expect(text(c)).toContain("Deploy is off: the policy server is Starting.");
  });

  test("ready: success badge, the facts of the deploy", async () => {
    const { host } = await open({ deploy: ok(ready()) });
    const c = card(host);
    expect(c.getAttribute("data-state")).toBe("ready");
    expect(c.querySelector(".ds-badge--success")).not.toBeNull();
    const facts = text(c.querySelector("[data-testid=marvel0-deploy-facts]"));
    expect(facts).toContain(SFT);
    expect(facts).toContain("pi05_fr3_all_state");
    expect(facts).toContain("0.22");
    expect(facts).toContain(UNIT);
  });

  test("the clients connected to the port are counted among the facts", async () => {
    const connected = ready({
      listener: { port: 8000, present: true, ours: true, established: 2 },
      stop_blockers: ["client_connected"],
    });
    const { host } = await open({ deploy: ok(connected) });
    const facts = text(
      host.querySelector("[data-testid=marvel0-deploy-facts]"),
    );
    expect(facts).toContain("Connected clients");
    expect(facts).toMatch(/Connected clients\s*2/);
    const quiet = await open({ deploy: ok(ready()) });
    expect(
      text(quiet.host.querySelector("[data-testid=marvel0-deploy-facts]")),
    ).not.toContain("Connected clients");
  });

  test("the state follows the server as it is read again", async () => {
    let now = starting();
    const { host } = await open({ deploy: () => ok(now) }, 20);
    expect(card(host).getAttribute("data-state")).toBe("starting");
    now = ready();
    await waitFor(() => card(host).getAttribute("data-state") === "ready", {
      label: "ready",
    });
  });

  test("Stop asks for its phrase and sends one request", async () => {
    const { host, calls } = await open({
      deploy: ok(ready()),
      stopDeploy: ok(stopped()),
    });
    await click(button(card(host), STOP)!);
    const d = dialog(host)!;
    expect(text(d)).toContain("Stop the policy server");
    expect(text(d)).toContain(UNIT);
    expect(text(d)).toContain("never kills a process by number or name");
    expect(button(d, STOP)!.disabled).toBe(true);
    await type(field(d, "Confirmation phrase"), "stop policy server");
    await click(button(d, STOP)!);
    await tick();
    expect(calls.stopDeploy).toHaveLength(1);
    expect(calls.stopDeploy[0]).toEqual({
      request_id: calls.stopDeploy[0].request_id,
      confirm: "stop policy server",
      client_override: "",
    });
    expect(
      text(host.querySelector("[data-testid=marvel0-deploy-result]")),
    ).toContain("The policy server is stopped");
  });

  test("with a client connected the stop needs a second phrase, and says what happens to the client", async () => {
    const connected = ready({
      listener: { port: 8000, present: true, ours: true, established: 1 },
      stop_blockers: ["client_connected"],
    });
    const { host, calls } = await open({
      deploy: ok(connected),
      stopDeploy: ok(stopped({ client_override_used: true })),
    });
    await click(button(card(host), STOP)!);
    const d = dialog(host)!;
    expect(text(d)).toContain("A client is connected");
    expect(text(d)).toContain("Keep a hand on the physical e-stop");
    await type(field(d, "Confirmation phrase"), "stop policy server");
    expect(button(d, STOP)!.disabled).toBe(true);
    await type(
      field(d, "Extra phrase for a connected client"),
      "stop with client connected",
    );
    expect(button(d, STOP)!.disabled).toBe(false);
    await click(button(d, STOP)!);
    await tick();
    expect(calls.stopDeploy[0]).toMatchObject({
      confirm: "stop policy server",
      client_override: "stop with client connected",
    });
    expect(
      text(host.querySelector("[data-testid=marvel0-deploy-result]")),
    ).toContain("lost its server");
  });

  test("a client that connects after the page last read is refused by the server and the dialog asks for the extra phrase", async () => {
    const { host } = await open({
      deploy: ok(ready()),
      stopDeploy: refused(409, "client_connected", { established: 1 }),
    });
    await click(button(card(host), STOP)!);
    await type(
      field(dialog(host)!, "Confirmation phrase"),
      "stop policy server",
    );
    await click(button(dialog(host)!, STOP)!);
    await tick();
    const d = dialog(host)!;
    expect(d).not.toBeNull();
    expect(text(d)).toContain("type the extra phrase to stop anyway");
    expect(field(d, "Extra phrase for a connected client")).not.toBeNull();
  });

  test("a stop the server could not finish says what the person runs", async () => {
    const { host } = await open({
      deploy: ok(ready()),
      stopDeploy: refused(502, "stop_incomplete", { unit: UNIT }),
    });
    await click(button(card(host), STOP)!);
    await type(
      field(dialog(host)!, "Confirmation phrase"),
      "stop policy server",
    );
    await click(button(dialog(host)!, STOP)!);
    await tick();
    expect(
      text(host.querySelector("[data-testid=marvel0-deploy-problem]")),
    ).toContain(`systemctl --user stop ${UNIT}`);
  });

  test("a deploy or stop that is already running keeps both buttons off", async () => {
    const running = ready({
      operation: { action: "stop", request_id: "abcdefgh", started_at: "t" },
    });
    const { host } = await open({ deploy: ok(running) });
    expect(button(card(host), STOP)!.disabled).toBe(true);
    expect(text(card(host))).toContain("A stop is running");
  });
});

describe("a dialog that is open when the state changes", () => {
  test("a deploy is no longer confirmable once a server is starting", async () => {
    let now: Record<string, unknown> = idle();
    const { host } = await open({ deploy: () => ok(now) }, 20);
    await click(button(card(host), "Deploy")!);
    await type(field(dialog(host)!, "Confirmation phrase"), `deploy ${SFT}`);
    expect(button(dialog(host)!, "Deploy")!.disabled).toBe(false);
    now = starting();
    await waitFor(() => card(host).getAttribute("data-state") === "starting", {
      label: "the new state",
    });
    expect(button(dialog(host)!, "Deploy")!.disabled).toBe(true);
  });

  test("a stop is no longer confirmable once there is nothing to stop", async () => {
    let now: Record<string, unknown> = ready();
    const { host } = await open({ deploy: () => ok(now) }, 20);
    await click(button(card(host), STOP)!);
    await type(
      field(dialog(host)!, "Confirmation phrase"),
      "stop policy server",
    );
    expect(button(dialog(host)!, STOP)!.disabled).toBe(false);
    now = idle();
    await waitFor(() => card(host).getAttribute("data-state") === "idle", {
      label: "the new state",
    });
    expect(button(dialog(host)!, STOP)!.disabled).toBe(true);
  });

  test("a state that could not be read again closes the dialog, so its button cannot be pressed", async () => {
    let fail = false;
    const { host } = await open(
      { deploy: () => (fail ? nothing() : ok(ready())) },
      20,
    );
    await click(button(card(host), STOP)!);
    await type(
      field(dialog(host)!, "Confirmation phrase"),
      "stop policy server",
    );
    fail = true;
    await waitFor(() => dialog(host) === null, {
      label: "the dialog to close",
    });
    expect(button(card(host), STOP)?.disabled ?? true).toBe(true);
  });
});

describe("what the page does not control", () => {
  test("a server started by hand is never offered a Stop: the person is told to press Ctrl+C", async () => {
    const byHand = idle({
      state: "foreign_listener",
      reason: { code: "hand_started", text: "English" },
      can_deploy: false,
      can_stop: false,
      listener: { port: 8000, present: true, ours: false, established: 0 },
      checks: checks({
        port_free: {
          level: "refuse",
          text: "E",
          params: { port: 8000, listeners: 1, ours: false },
        },
      }),
      live_annotation: {
        blocked: true,
        text: "server holds ? MiB, live annotation cannot start",
      },
    });
    const { host } = await open({ deploy: ok(byHand) });
    const c = card(host);
    expect(c.getAttribute("data-state")).toBe("foreign_listener");
    expect(button(c, STOP)).toBeUndefined();
    expect(text(c)).toContain("press Ctrl+C in the terminal that runs it");
    expect(button(c, "Deploy")!.disabled).toBe(true);
    expect(text(c)).toContain("how much is not known");
  });

  test("when the client may have been cut off the hand-on-the-e-stop notice is on, as an alert", async () => {
    const failed = idle({
      state: "failed",
      deploy: deployInfo(),
      failure: { kind: "traceback", text: "English" },
      estop: { always: true, keep_hand: true, text: "x" },
    });
    const { host } = await open({ deploy: ok(failed) });
    const notice = host.querySelector(
      "[data-testid=marvel0-deploy-keep-hand]",
    )!;
    expect(text(notice)).toContain("Keep a hand on the physical e-stop");
    expect(text(notice)).toContain("Failed");
    expect(notice.querySelector("[role=alert]")).not.toBeNull();
    expect(text(card(host))).toContain(
      "The server stopped with an error; the log shows it.",
    );
  });

  test("the notice stays on an idle card after a stop that cut a client off", async () => {
    const after = idle({
      deploy: deployInfo({ phase: "stopped" }),
      estop: { always: true, keep_hand: true, text: "English" },
    });
    const { host } = await open({ deploy: ok(after) });
    expect(card(host).getAttribute("data-state")).toBe("idle");
    const notice = host.querySelector(
      "[data-testid=marvel0-deploy-keep-hand]",
    )!;
    expect(text(notice)).toContain("Keep a hand on the physical e-stop");
  });

  test("no such notice while nothing suggests a client was cut off", async () => {
    const { host } = await open();
    expect(
      host.querySelector("[data-testid=marvel0-deploy-keep-hand]"),
    ).toBeNull();
  });

  test("an unreadable state keeps both buttons off and says so", async () => {
    const { host } = await open({ deploy: nothing("fetch failed") });
    const c = card(host);
    expect(text(c)).toContain(en["marvel0.deploy.unreadable"]);
    expect(text(c)).toContain("fetch failed");
    expect(button(c, "Deploy")!.disabled).toBe(true);
    expect(button(c, STOP)).toBeUndefined();
  });

  test("an answer in a format the page does not know keeps both buttons off", async () => {
    const { host } = await open({
      deploy: ok({ schema: "levi.marvel0.deploy.v9" }),
    });
    expect(text(card(host))).toContain(en["marvel0.deploy.format"]);
    expect(button(card(host), "Deploy")!.disabled).toBe(true);
  });

  test("an answer the parser chokes on is a format problem, and the reading goes on", async () => {
    const poison = {
      get schema(): string {
        throw new Error("boom");
      },
    };
    const { host, calls } = await open({ deploy: ok(poison) }, 20);
    expect(text(card(host))).toContain(en["marvel0.deploy.format"]);
    const before = calls.deploy;
    await waitFor(() => calls.deploy > before, { label: "another read" });
  });

  test("a read that fails after a good one switches the buttons off at once", async () => {
    let calls = 0;
    const { host } = await open(
      { deploy: () => (calls++ === 0 ? ok(ready()) : nothing()) },
      20,
    );
    await waitFor(
      () => text(card(host)).includes(en["marvel0.deploy.unreadable"]),
      {
        label: "the unreadable notice",
      },
    );
    expect(button(card(host), STOP)?.disabled ?? true).toBe(true);
    expect(button(card(host), "Deploy")!.disabled).toBe(true);
  });

  test("an idle marvel0 after a stop says nothing about the deploy that ended", async () => {
    const after = idle({
      deploy: deployInfo({ phase: "stopped" }),
      readiness: {
        norm_stats: true,
        rtc_enabled: true,
        rtc_warmup: true,
        cfg: null,
        listening: true,
      },
    });
    const { host } = await open({ deploy: ok(after) });
    const c = card(host);
    expect(c.getAttribute("data-state")).toBe("idle");
    expect(c.querySelector("[data-testid=marvel0-deploy-ready]")).toBeNull();
    expect(
      text(c.querySelector("[data-testid=marvel0-deploy-facts]")),
    ).not.toContain(UNIT);
    expect(button(c, "Deploy")!.disabled).toBe(false);
    expect(button(c, STOP)).toBeUndefined();
  });

  test("a state the page does not know is shown as unknown, never as idle", async () => {
    const { host } = await open({
      deploy: ok(idle({ state: "paused", can_deploy: true })),
    });
    expect(card(host).getAttribute("data-state")).toBe("unknown");
    expect(button(card(host), "Deploy")!.disabled).toBe(true);
  });
});

describe("the checks and the log", () => {
  test("every check is listed with its words, the blocking ones first in the summary", async () => {
    const blocked = idle({
      can_deploy: false,
      checks: checks({
        arm_lock: { level: "refuse", text: "E", params: {} },
        gpu_lock: { level: "unknown", text: "E", params: {} },
      }),
    });
    const { host } = await open({ deploy: ok(blocked) });
    const c = card(host);
    const items = Array.from(
      c.querySelectorAll("[data-testid=marvel0-deploy-checks] li"),
    );
    expect(items).toHaveLength(9);
    expect(text(c)).toContain("2 check(s) block a deploy.");
    expect(text(items[5])).toContain("Arm lock");
    expect(text(items[5])).toContain(
      "The arm lock is held: a run is using the arm.",
    );
    expect(text(items[6])).toContain("Cannot tell");
    expect(button(c, "Deploy")!.disabled).toBe(true);
    expect(text(c)).toContain("Deploy is off:");
  });

  test("the list of checks is open when something blocks a deploy that may be wanted, and folded when the server of this page runs (it blocks a second one by itself)", async () => {
    const blocks = {
      port_free: { level: "refuse", text: "E", params: {} },
      policy_units: { level: "refuse", text: "E", params: {} },
    };
    const idleBlocked = await open({
      deploy: ok(idle({ can_deploy: false, checks: checks(blocks) })),
    });
    const fold = (host: HTMLElement) =>
      host.querySelector("[data-testid=marvel0-deploy-checks]")!
        .parentElement as HTMLDetailsElement;
    expect(fold(idleBlocked.host).open).toBe(true);
    const running = await open({
      deploy: ok(ready({ can_deploy: false, checks: checks(blocks) })),
    });
    expect(fold(running.host).open).toBe(false);
    expect(text(card(running.host))).toContain("2 check(s) block a deploy.");
  });

  test("all checks passing is said once", async () => {
    const { host } = await open();
    expect(text(card(host))).toContain("All 9 checks pass.");
  });

  test("the log of a failed deploy is shown as the unit wrote it, in English", async () => {
    const failed = idle({
      state: "failed",
      deploy: deployInfo(),
      failure: { kind: "xla_oom", text: "English" },
    });
    const { host } = await open({ deploy: ok(failed) });
    await waitFor(
      () => host.querySelector("[data-testid=marvel0-deploy-log]"),
      {
        label: "the log",
      },
    );
    const log = host.querySelector("[data-testid=marvel0-deploy-log]")!;
    expect(log.getAttribute("lang")).toBe("en");
    expect(log.tagName).toBe("PRE");
    expect(log.textContent).toContain(
      "Using norm stats shipped with the checkpoint",
    );
    // The log of a deploy that failed is open: that is where the reason is.
    const details = host.querySelector(
      "[data-testid=marvel0-deploy-log-details]",
    ) as HTMLElement;
    expect(details.hasAttribute("open")).toBe(true);
    expect(text(card(host))).toContain(
      "The GPU ran out of memory while loading the model",
    );
  });

  test("the log of a server that runs starts closed", async () => {
    const { host } = await open({ deploy: ok(ready()) });
    const details = host.querySelector(
      "[data-testid=marvel0-deploy-log-details]",
    ) as HTMLElement;
    expect(details.hasAttribute("open")).toBe(false);
  });

  test("the log of a ready server is read only once the person opens it", async () => {
    const { host, calls } = await open({ deploy: ok(ready()) });
    expect(calls.log).toBe(0);
    const details = host.querySelector(
      "[data-testid=marvel0-deploy-log-details]",
    ) as HTMLDetailsElement;
    await act(async () => {
      details.open = true;
      details.dispatchEvent(new Event("toggle"));
    });
    await waitFor(() => calls.log > 0, { label: "the log to be asked for" });
    await waitFor(
      () => host.querySelector("[data-testid=marvel0-deploy-log]"),
      {
        label: "the log",
      },
    );
  });

  test("a log that cannot be read is said, and does not hide the state", async () => {
    const failed = idle({
      state: "failed",
      deploy: deployInfo(),
      failure: { kind: "exited", text: "E" },
    });
    const { host } = await open({ deploy: ok(failed), log: nothing() });
    await waitFor(
      () => text(card(host)).includes(en["marvel0.deploy.log.unreadable"]),
      {
        label: "the log notice",
      },
    );
    expect(card(host).getAttribute("data-state")).toBe("failed");
  });

  test("the note about the source files is worded", async () => {
    const { host } = await open();
    expect(text(card(host))).toContain(
      en["marvel0.deploy.note.source_hash_unmanifested"],
    );
  });
});

describe("polling", () => {
  test("reads the state every 3 s by default, and not while the tab is hidden", async () => {
    const real = globalThis.setTimeout;
    const delays: number[] = [];
    globalThis.setTimeout = ((
      fn: () => void,
      ms?: number,
      ...rest: unknown[]
    ) => {
      if (typeof ms === "number" && ms >= 1000) delays.push(ms);
      return real(fn as never, ms, ...(rest as []));
    }) as typeof setTimeout;
    try {
      const { api } = fakeApi();
      await render(<Marvel0Actions api={api} />);
      await tick(5);
      expect(delays).toContain(3000);
    } finally {
      globalThis.setTimeout = real;
    }
  });

  test("a hidden tab stops reading, and coming back reads at once", async () => {
    const { host, calls } = await open({}, 20);
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => "hidden",
    });
    document.dispatchEvent(new Event("visibilitychange"));
    await tick(2, 5);
    const hidden = calls.deploy;
    await tick(5, 30);
    expect(calls.deploy).toBe(hidden);
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => "visible",
    });
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await waitFor(() => calls.deploy > hidden, { label: "a read on return" });
    expect(host).toBeDefined();
  });
});

describe("a state that is too old", () => {
  test("is dropped when the reads keep failing, so the card never shows it as current", async () => {
    const realNow = Date.now;
    let offset = 0;
    Date.now = () => realNow() + offset;
    try {
      let calls = 0;
      const { host } = await open(
        { deploy: () => (calls++ === 0 ? ok(ready()) : nothing()) },
        20,
      );
      // The failed read keeps the last good one for a while, marked as not current.
      await waitFor(
        () => text(card(host)).includes(en["marvel0.deploy.unreadable"]),
        { label: "the notice" },
      );
      expect(card(host).getAttribute("data-state")).toBe("ready");
      offset = 11_000;
      await waitFor(() => card(host).getAttribute("data-state") === "unknown", {
        label: "the old state to be dropped",
      });
      expect(text(card(host))).not.toContain("Ready");
      expect(button(card(host), STOP)).toBeUndefined();
    } finally {
      Date.now = realNow;
    }
  });

  test("a hidden tab gives up the read in flight", async () => {
    const hang = () => new Promise<never>(() => {});
    const { api, calls } = fakeApi({ deploy: hang });
    await render(<Marvel0Actions api={api} pollMs={20} />);
    await waitFor(() => calls.signals.length > 0, { label: "a read" });
    const signal = calls.signals[0]!;
    expect(signal.aborted).toBe(false);
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => "hidden",
    });
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(signal.aborted).toBe(true);
  });
});

describe("the catalog", () => {
  test("a model list that could not be read is read again when the tab returns", async () => {
    let count = 0;
    const { host, calls } = await open({
      models: () => (count++ === 0 ? nothing() : ok(modelsAnswer())),
    });
    expect(text(host)).toContain(en["marvel0.act.models_failed"]);
    expect(calls.models).toBe(1);
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => "visible",
    });
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await waitFor(() => calls.models === 2, { label: "a second read" });
    await waitFor(() => !text(host).includes(en["marvel0.act.models_failed"]), {
      label: "the notice to go",
    });
  });

  test("a model list that was read is not read again just because the tab returns", async () => {
    const { calls } = await open();
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => "visible",
    });
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await tick(3, 5);
    expect(calls.models).toBe(1);
  });

  test("Reload reads the models and the site again", async () => {
    const { host, calls } = await open({ models: nothing() });
    await click(button(host, "Reload the models")!);
    await tick();
    expect(calls.models).toBe(2);
    expect(calls.site).toBe(2);
  });

  test("a model that takes only the known task instructions says so", async () => {
    const { host } = await open({
      models: ok(
        modelsAnswer({ models: [model({ restricted_prompts: true })] }),
      ),
    });
    expect(text(card(host))).toContain(
      "Takes only the known task instructions",
    );
  });
});

describe("the command card follows the state only while it is current", () => {
  test("a state that could not be read again is not used to say the server fits", async () => {
    let calls = 0;
    const { host } = await open(
      { deploy: () => (calls++ === 0 ? ok(ready()) : nothing()) },
      20,
    );
    await waitFor(
      () =>
        host
          .querySelector("[data-testid=marvel0-command-server]")
          ?.getAttribute("data-kind") === "unknown",
      { label: "the command card to stop trusting the state" },
    );
  });
});

describe("the site file", () => {
  const MISSING = refused(
    412,
    "site_missing",
    {
      looked_at: "/x/lab/marvel0/site.toml",
      template: '# LEVI marvel0 site file\nschema = "levi.marvel0.site.v1"\n',
    },
    "marvel0 site file missing",
  );

  test("without it both cards are off, and the page says where it looked and shows the template", async () => {
    const { host } = await open(
      { models: MISSING, site: MISSING, deploy: MISSING },
      5000,
      false,
      false,
    );
    await waitFor(
      () => host.querySelector("[data-testid=marvel0-site-issue]"),
      {
        label: "the notice",
      },
    );
    const issue = host.querySelector("[data-testid=marvel0-site-issue]")!;
    expect(text(issue)).toContain("/x/lab/marvel0/site.toml");
    expect(text(issue)).toContain("The marvel0 site file is missing");
    const template = issue.querySelector("pre")!;
    expect(template.getAttribute("lang")).toBe("en");
    expect(template.textContent).toContain("levi.marvel0.site.v1");
    expect(host.querySelector("[data-testid=marvel0-deploy]")).toBeNull();
    expect(host.querySelector("[data-testid=marvel0-command]")).toBeNull();
    expect(button(host, "Deploy")).toBeUndefined();
  });

  test("an invalid file lists the fields to fix", async () => {
    const invalid = refused(412, "site_invalid", {
      errors: [{ field: "paths.python", code: "not_absolute" }],
    });
    const { host } = await open(
      { models: invalid, site: invalid, deploy: invalid },
      5000,
      false,
      false,
    );
    await waitFor(
      () => host.querySelector("[data-testid=marvel0-site-issue]"),
      {
        label: "the notice",
      },
    );
    const issue = host.querySelector("[data-testid=marvel0-site-issue]")!;
    expect(text(issue)).toContain("paths.python");
    expect(text(issue)).toContain("not_absolute");
    expect(host.querySelector("[data-testid=marvel0-deploy]")).toBeNull();
  });
});

describe("in Chinese", () => {
  test("the card, a refusal and a failure are worded without an English sentence", async () => {
    const failed = idle({
      state: "failed",
      deploy: deployInfo(),
      failure: { kind: "rtc_not_ready", text: "ENGLISH FAILURE TEXT" },
      reason: { code: "exited_after_ready", text: "ENGLISH REASON TEXT" },
      estop: { always: true, keep_hand: true, text: "ENGLISH ESTOP TEXT" },
    });
    const { host } = await open(
      {
        deploy: ok(failed),
        startDeploy: refused(
          409,
          "gpu_insufficient",
          {
            fraction: 0.3,
            total_mib: 32607,
            free_mib: 5100,
            required_mib: 10807,
            reserved_mib: 9783,
            margin_mib: 1024,
          },
          "ENGLISH REFUSAL TEXT",
        ),
      },
      5000,
      true,
    );
    await waitFor(() => text(card(host)).includes(zh["marvel0.deploy.title"]), {
      label: "Chinese",
    });
    const c = card(host);
    const body = text(c);
    expect(body).toContain(zh["marvel0.deploy.scope"]);
    expect(body).toContain(zh["marvel0.deploy.state.failed"]);
    expect(body).toContain(zh["marvel0.deploy.failure.rtc_not_ready"]);
    expect(body).toContain(zh["marvel0.deploy.reason.exited_after_ready"]);
    for (const english of [
      "ENGLISH FAILURE TEXT",
      "ENGLISH REASON TEXT",
      "ENGLISH ESTOP TEXT",
    ])
      expect(body).not.toContain(english);
    // Only the log, the identifiers and the units are English.
    await click(button(c, zh["marvel0.deploy.btn.deploy"])!);
    await type(
      field(dialog(host)!, zh["marvel0.deploy.dialog.phrase"]),
      `deploy ${SFT}`,
    );
    await click(button(dialog(host)!, zh["marvel0.deploy.btn.deploy"])!);
    await tick();
    const problem = text(
      host.querySelector("[data-testid=marvel0-deploy-problem]"),
    );
    expect(problem).toContain("10807");
    expect(problem).toContain("显存比例");
    expect(problem).not.toContain("ENGLISH REFUSAL TEXT");
  });

  test("a code the page does not know shows the server's sentence, marked as English", async () => {
    const { host } = await open(
      {
        startDeploy: refused(409, "brand_new_refusal", {}, "Brand new refusal"),
      },
      5000,
      true,
    );
    await waitFor(() => text(card(host)).includes(zh["marvel0.deploy.title"]), {
      label: "Chinese",
    });
    await click(button(card(host), zh["marvel0.deploy.btn.deploy"])!);
    await type(
      field(dialog(host)!, zh["marvel0.deploy.dialog.phrase"]),
      `deploy ${SFT}`,
    );
    await click(button(dialog(host)!, zh["marvel0.deploy.btn.deploy"])!);
    await tick();
    const problem = host.querySelector("[data-testid=marvel0-deploy-problem]")!;
    expect(problem.querySelector("[lang=en]")?.textContent).toBe(
      "Brand new refusal",
    );
  });
});

describe("the labels and what a lost state takes with it", () => {
  test("the stop buttons name the policy server, in both languages", async () => {
    expect(STOP).toBe("Stop policy server");
    expect(zh["marvel0.deploy.btn.stop"]).toBe("停止策略服务器");
    const { host } = await open({ deploy: ok(ready()) });
    await click(button(card(host), STOP)!);
    expect(button(dialog(host)!, STOP)).toBeDefined();
    for (const node of host.querySelectorAll("button"))
      expect(node.textContent).not.toMatch(/e-?stop|emergency/i);
  });

  test("a stopping state the server says can be stopped again offers the button", async () => {
    const stale = ready({
      state: "stopping",
      can_stop: true,
      deploy: deployInfo({ phase: "stopping" }),
    });
    const { host } = await open({ deploy: ok(stale) });
    expect(card(host).getAttribute("data-state")).toBe("stopping");
    expect(button(card(host), STOP)!.disabled).toBe(false);
    const going = ready({ state: "stopping", can_stop: false });
    const other = await open({ deploy: ok(going) });
    expect(button(card(other.host), STOP)).toBeUndefined();
  });

  test("a dialog does not come back with its typed phrase after the state was lost", async () => {
    let lost = false;
    const { host } = await open(
      { deploy: () => (lost ? nothing() : ok(idle())) },
      20,
    );
    await click(button(card(host), "Deploy")!);
    await type(field(dialog(host)!, "Confirmation phrase"), `deploy ${SFT}`);
    expect(button(dialog(host)!, "Deploy")!.disabled).toBe(false);
    lost = true;
    await waitFor(() => dialog(host) === null, {
      label: "the dialog to close with the state",
    });
    lost = false;
    await waitFor(
      () => card(host).getAttribute("data-state") === "idle" && !!card(host),
      { label: "the state to be read again" },
    );
    await tick(3, 40);
    expect(dialog(host)).toBeNull();
    await click(button(card(host), "Deploy")!);
    expect(field(dialog(host)!, "Confirmation phrase")!.value).toBe("");
    expect(button(dialog(host)!, "Deploy")!.disabled).toBe(true);
  });

  test("the acceptance of an unverified model is not kept for the next deploy", async () => {
    const { host } = await open();
    const c = card(host);
    await choose(field<HTMLSelectElement>(c, "Model"), CFG);
    const label =
      "I accept deploying a model without a recorded real-GPU check";
    const box = () => field(dialog(host)!, label)!;
    await click(button(c, "Deploy")!);
    await click(box());
    expect(box().checked).toBe(true);
    await click(button(dialog(host)!, "Cancel")!);
    await click(button(c, "Deploy")!);
    expect(box().checked).toBe(false);
  });

  test("an unreadable state is said once, then the cause", async () => {
    const { host } = await open({ deploy: nothing("fetch failed") });
    const body = text(card(host));
    expect(body.split(en["marvel0.deploy.unreadable"]).length - 1).toBe(1);
    expect(body).toContain("fetch failed");
  });

  test("an answer in an unknown format is said once, without the generic sentence", async () => {
    const { host } = await open({
      deploy: ok({ schema: "levi.marvel0.deploy.v9" }),
    });
    const body = text(card(host));
    expect(body.split(en["marvel0.deploy.format"]).length - 1).toBe(1);
    expect(body).not.toContain(en["marvel0.deploy.unreadable"]);
  });

  test("a write that was accepted but not understood does not say the buttons are off", async () => {
    const { host } = await open({
      startDeploy: ok({ schema: "levi.marvel0.deploy_result.v9" }, 202),
    });
    await click(button(card(host), "Deploy")!);
    await type(field(dialog(host)!, "Confirmation phrase"), `deploy ${SFT}`);
    await click(button(dialog(host)!, "Deploy")!);
    await tick();
    const shown = text(
      host.querySelector("[data-testid=marvel0-deploy-result]"),
    );
    expect(shown).toContain(en["marvel0.deploy.accepted_unread"]);
    expect(shown).not.toContain("Deploy and Stop are off");
    expect(zh["marvel0.deploy.accepted_unread"]).toMatch(/[一-鿿]/);
  });
});

// Keep the fixtures imported for the shapes used above.
void model;
void cfgModel;
