import {
  click,
  render,
  setupDom,
  waitFor,
} from "@/components/ds/__tests__/dom";
import { afterEach, describe, expect, test } from "bun:test";
import { modelsAnswer, siteAnswer } from "./catalog-fixtures";
import { preview } from "./command-fixtures";
import { RUN, STACK, runDoc, stackDoc } from "./ctl-fixtures";
import { deployInfo, idle, logAnswer, ready } from "./deploy-fixtures";
import { quietMarvel0, quietSetup } from "./fixtures";
import { button, choose, field, tick } from "./actions-helpers";

const { default: Marvel0Page } = await import("@/app/marvel0/page");
setupDom();

const SFT = "pi05_fr3_all_step49999";
const CFG = "recap_cfg_r2_best_step14300_jax";
const P = "/api/levi/marvel0";

let restore = () => {};
afterEach(() => {
  restore();
  restore = () => {};
  try {
    localStorage.clear();
  } catch {
    // no storage in this DOM
  }
});

function core(deploy: unknown, run: unknown = runDoc("idle_ready")) {
  const original = globalThis.fetch;
  const calls: Array<{ path: string; init?: RequestInit }> = [];
  const answers: Record<string, unknown> = {
    "/api/levi/setup/status": quietSetup(),
    [`${P}/status`]: quietMarvel0(),
    [`${P}/models`]: modelsAnswer(),
    [`${P}/site`]: siteAnswer(),
    [`${P}/deploy`]: deploy,
    [`${P}/deploy/log?lines=80`]: logAnswer(),
    [STACK]: stackDoc("idle"),
    [RUN]: run,
  };
  globalThis.fetch = (async (input: unknown, init?: RequestInit) => {
    const path = String(input);
    calls.push({ path, init });
    if (init?.method === "POST") {
      if (path === `${P}/command/preview`)
        return new Response(JSON.stringify(preview()), { status: 200 });
      return new Response("{}", { status: 404 });
    }
    return path in answers
      ? new Response(JSON.stringify(answers[path]), { status: 200 })
      : new Response("{}", { status: 404 });
  }) as typeof fetch;
  restore = () => {
    globalThis.fetch = original;
  };
  return calls;
}
const loaded = (host: HTMLElement) =>
  waitFor(
    () => host.querySelector("[data-testid=marvel0-deploy][data-state]"),
    {
      label: "the policy server card",
    },
  );
const policy = (host: HTMLElement) =>
  host.querySelector("[data-testid=marvel0-deploy]") as HTMLElement;
const follow = (host: HTMLElement) =>
  host.querySelector("[data-testid=marvel0-command-follow]") as HTMLElement;

describe("one selected model for the whole page", () => {
  test("the model chosen in the policy server card is the command card's, at once, with no model control of its own", async () => {
    core(idle());
    const { host } = await render(<Marvel0Page />);
    await loaded(host);
    expect(follow(host).textContent).toContain(
      `follows the policy server: ${SFT}`,
    );
    await choose(field<HTMLSelectElement>(policy(host), "Model"), CFG);
    expect(follow(host).textContent).toContain(
      `follows the policy server: ${CFG}`,
    );
    expect(follow(host).getAttribute("data-source")).toBe("choice");
    // The command card has no select and no switch.
    const command = host.querySelector("[data-testid=marvel0-command]")!;
    expect(command.querySelectorAll("select")).toHaveLength(0);
    expect(command.querySelector("[role=switch]")).toBeNull();
    // The default rollout group carries the full checkpoint name.
    expect(
      (field(command as HTMLElement, "Rollout group") as HTMLInputElement)
        .placeholder,
    ).toBe(`${CFG}_rtc_demo`);
  });

  test("RTC chosen in the policy server card changes the command's horizon and group", async () => {
    core(idle());
    const { host } = await render(<Marvel0Page />);
    await loaded(host);
    const command = host.querySelector(
      "[data-testid=marvel0-command]",
    ) as HTMLElement;
    expect(field(command, "RTC execute horizon")).not.toBeNull();
    await click(field(policy(host), "RTC (real-time chunking)")!);
    expect(field(command, "Open-loop horizon")).not.toBeNull();
    expect(follow(host).textContent).toContain("RTC off");
  });

  test("the command made for the selection asks for that model", async () => {
    const calls = core(idle());
    const { host } = await render(<Marvel0Page />);
    await loaded(host);
    await choose(field<HTMLSelectElement>(policy(host), "Model"), CFG);
    const original = globalThis.fetch;
    const bodies: unknown[] = [];
    globalThis.fetch = (async (input: unknown, init?: RequestInit) => {
      if (init?.method === "POST") {
        bodies.push(JSON.parse(String(init.body)));
        return new Response(JSON.stringify(preview()), { status: 200 });
      }
      return original(input as string, init);
    }) as typeof fetch;
    await click(
      button(
        host.querySelector("[data-testid=marvel0-command]") as HTMLElement,
        "Make the command",
      )!,
    );
    await tick(3, 10);
    expect(bodies[0]).toMatchObject({ model_id: CFG, rtc: true });
    void calls;
  });

  test("while this page's server runs a model, that model, its RTC and its strength are the command's, and the policy card's controls are read-only", async () => {
    core(
      ready({
        deploy: deployInfo({
          model_id: CFG,
          rtc: false,
          cfg_guidance_scale: 1.5,
        }),
      }),
    );
    const { host } = await render(<Marvel0Page />);
    await loaded(host);
    await waitFor(
      () => follow(host)?.getAttribute("data-source") === "server",
      {
        label: "the follow badge",
      },
    );
    expect(follow(host).textContent).toContain(
      `follows the policy server: ${CFG}`,
    );
    expect(follow(host).textContent).toContain("RTC off");
    expect(follow(host).textContent).toContain("Guidance strength 1.5");
    const model = field<HTMLSelectElement>(policy(host), "Model")!;
    expect(model.value).toBe(CFG);
    expect(model.disabled).toBe(true);
    expect(
      (field(policy(host), "RTC (real-time chunking)") as HTMLInputElement)
        .disabled,
    ).toBe(true);
    expect(
      host.querySelector("[data-testid=marvel0-deploy-held]"),
    ).not.toBeNull();
    // The old warnings cannot occur: the command fits the server.
    expect(
      host
        .querySelector("[data-testid=marvel0-command-server]")!
        .getAttribute("data-kind"),
    ).toBe("match");
  });

  test("use another model takes the keyboard to the policy server card's model control", async () => {
    core(idle());
    const { host } = await render(<Marvel0Page />);
    await loaded(host);
    await click(button(host, "use another model")!);
    expect(document.activeElement).toBe(field(policy(host), "Model"));
  });
});

describe("the Run card and starting from the page", () => {
  test("the command card is step 3 and the Run card step 4; Start the client appears with the command", async () => {
    core(idle(), runDoc("idle_ready"));
    const { host } = await render(<Marvel0Page />);
    await loaded(host);
    await waitFor(
      () => host.querySelector("[data-testid=marvel0-run][data-state]"),
      { label: "the run card" },
    );
    const command = host.querySelector(
      "[data-testid=marvel0-command]",
    ) as HTMLElement;
    expect(button(command, "Start the client")).toBeUndefined();
    await click(button(command, "Make the command")!);
    await tick(3, 10);
    expect(button(command, "Start the client")).toBeDefined();
    // The copy stays as the manual route.
    expect(button(command, "Copy")).toBeDefined();
  });

  test("'Robot idle?' is one folded line, and is left out while this page's own run is live", async () => {
    core(idle(), runDoc("idle_ready"));
    const quiet = await render(<Marvel0Page />);
    await loaded(quiet.host);
    const banner = quiet.host.querySelector(
      "[data-testid=marvel0-idle]",
    ) as HTMLDetailsElement;
    expect(banner.tagName).toBe("DETAILS");
    expect(banner.open).toBe(false);
    restore();
    core(idle(), runDoc("running"));
    const live = await render(<Marvel0Page />);
    await loaded(live.host);
    await waitFor(
      () =>
        live.host.querySelector(
          "[data-testid=marvel0-run][data-state=running]",
        ),
      { label: "the live run" },
    );
    await tick(3, 10);
    expect(live.host.querySelector("[data-testid=marvel0-idle]")).toBeNull();
  });

  test("the services of this machine are one fold, closed while none of them is red", async () => {
    core(idle());
    const { host } = await render(<Marvel0Page />);
    await loaded(host);
    const fold = host.querySelector(
      "[data-testid=marvel0-host]",
    ) as HTMLDetailsElement;
    expect(fold.tagName).toBe("DETAILS");
    expect(fold.querySelector("summary h2")).not.toBeNull();
    expect(fold.open).toBe(false);
    expect(fold.querySelector("[data-testid=marvel0-card-gpu]")).not.toBeNull();
  });

  test("the idle Run card points at step 3, and the go-to button moves the keyboard to the command card", async () => {
    core(idle(), runDoc("idle_ready"));
    const { host } = await render(<Marvel0Page />);
    await loaded(host);
    await waitFor(() => host.querySelector("[data-testid=marvel0-run-how]"), {
      label: "the how-to-start line",
    });
    const how = host.querySelector(
      "[data-testid=marvel0-run-how]",
    ) as HTMLElement;
    await click(button(how, "Go to step 3")!);
    expect((document.activeElement as HTMLElement | null)?.id).toBe(
      "marvel0-command-card",
    );
  });

  test("the whole page: the chain first, then steps 1 to 4 in order", async () => {
    core(idle());
    const { host } = await render(<Marvel0Page />);
    await loaded(host);
    const order = [
      "[data-testid=marvel0-strip]",
      "[data-testid=marvel0-step-arm]",
      "[data-testid=marvel0-deploy]",
      "[data-testid=marvel0-command]",
      "[data-testid=marvel0-step-run]",
      "[data-testid=marvel0-run]",
    ].map((sel) => host.querySelector(sel)!);
    for (let i = 1; i < order.length; i += 1)
      expect(
        order[i - 1].compareDocumentPosition(order[i]) &
          Node.DOCUMENT_POSITION_FOLLOWING,
      ).toBeTruthy();
  });
});
