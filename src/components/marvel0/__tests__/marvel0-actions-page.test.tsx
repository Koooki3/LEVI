import {
  click,
  render,
  setupDom,
  waitFor,
} from "@/components/ds/__tests__/dom";
import { afterEach, describe, expect, test } from "bun:test";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import { LocaleProvider } from "@/components/levi-locale";
import { modelsAnswer, siteAnswer } from "./catalog-fixtures";
import { preview } from "./command-fixtures";
import { deployed, idle, logAnswer, ready } from "./deploy-fixtures";
import { quietMarvel0, quietSetup } from "./fixtures";
import { button, tick } from "./actions-helpers";
import { RUN, STACK, runDoc, stackDoc } from "./ctl-fixtures";

const { default: Marvel0Page } = await import("@/app/marvel0/page");
setupDom();

const SETUP = "/api/levi/setup/status";
const MARVEL0 = "/api/levi/marvel0/status";
const READS = [
  SETUP,
  MARVEL0,
  "/api/levi/marvel0/models",
  "/api/levi/marvel0/site",
  "/api/levi/marvel0/deploy",
  "/api/levi/marvel0/deploy/log?lines=80",
  STACK,
  RUN,
];

type Call = { path: string; init?: RequestInit };
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

/** The core as the page meets it: every route answers from `answers`; a write
 * answers from `writes`. A route that is not listed is a 404. */
function core(
  answers: Record<string, unknown>,
  writes: Record<string, { status: number; body: unknown }> = {},
) {
  const original = globalThis.fetch;
  const calls: Call[] = [];
  globalThis.fetch = (async (input: unknown, init?: RequestInit) => {
    const path = String(input);
    calls.push({ path, init });
    if (init?.method === "POST") {
      const write = writes[path];
      if (!write) return new Response("{}", { status: 404 });
      return new Response(JSON.stringify(write.body), { status: write.status });
    }
    if (!(path in answers)) return new Response("{}", { status: 404 });
    return new Response(JSON.stringify(answers[path]), { status: 200 });
  }) as typeof fetch;
  restore = () => {
    globalThis.fetch = original;
  };
  return calls;
}
const healthy = (deploy: unknown = idle()) => ({
  [SETUP]: quietSetup(),
  [MARVEL0]: quietMarvel0(),
  "/api/levi/marvel0/models": modelsAnswer(),
  "/api/levi/marvel0/site": siteAnswer(),
  "/api/levi/marvel0/deploy": deploy,
  "/api/levi/marvel0/deploy/log?lines=80": logAnswer(),
  [STACK]: stackDoc("idle"),
  [RUN]: runDoc("idle_ready"),
});
const loaded = (host: HTMLElement) =>
  waitFor(
    () => host.querySelector("[data-testid=marvel0-deploy][data-state]"),
    {
      label: "the policy server card",
    },
  );

describe("the marvel0 with its action cards", () => {
  test("opening the page only reads: every request is a plain GET, and nothing is written", async () => {
    const calls = core(healthy());
    const { host } = await render(<Marvel0Page />);
    await loaded(host);
    await tick(3, 5);
    // The status routes, the policy server's, the stack's and the client's,
    // and the two of step 5 (readiness, and the hosted terminal D); the log is
    // read only while it is wanted.
    expect(new Set(calls.map((call) => call.path))).toEqual(
      new Set([
        ...READS.slice(0, 5),
        STACK,
        RUN,
        "/api/levi/marvel0/aeri",
        "/api/levi/marvel0/aeri/run",
      ]),
    );
    for (const call of calls) {
      expect(call.init?.method ?? "GET").toBe("GET");
      expect(call.init?.body).toBeUndefined();
      expect(call.init?.cache).toBe("no-store");
    }
  });

  test("the action cards sit in steps 2 and 3 next to their status cards, above the services of this machine", async () => {
    core(healthy());
    const { host } = await render(<Marvel0Page />);
    await loaded(host);
    expect(host.querySelector("[data-testid=marvel0-idle]")).not.toBeNull();
    expect(
      host.querySelector("[data-testid=marvel0-card-policy]"),
    ).not.toBeNull();
    const actions = host.querySelector("[data-testid=marvel0-actions]")!;
    // Below the arm stack (step 1), above the GPU and the other services.
    expect(
      actions.compareDocumentPosition(
        host.querySelector("[data-testid=marvel0-card-gpu]")!,
      ) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      actions.compareDocumentPosition(
        host.querySelector("[data-testid=marvel0-card-arm]")!,
      ) & Node.DOCUMENT_POSITION_PRECEDING,
    ).toBeTruthy();
    expect(
      actions.querySelector("[data-testid=marvel0-card-policy]"),
    ).not.toBeNull();
    expect(
      actions.querySelector("[data-testid=marvel0-card-demo]"),
    ).not.toBeNull();
    const steps = Array.from(host.querySelectorAll(".rg-step__title")).map(
      (h) => h.textContent,
    );
    // "Step n" and the name: the steps (1-5) are told apart from the chain's
    // node numbers (01-07), and the run is the fourth step of its own.
    const step = (n: number, name: string) =>
      `${en["marvel0.step.n"].replace("{n}", String(n))}${name}`;
    expect(steps).toEqual([
      step(1, en["marvel0.step.arm"]),
      step(2, en["marvel0.step.policy"]),
      step(3, en["marvel0.step.client"]),
      step(4, en["marvel0.step.run"]),
      step(5, en["marvel0.autonomy.step"]),
    ]);
    expect(
      actions.querySelector("[data-testid=marvel0-deploy]"),
    ).not.toBeNull();
    expect(
      actions.querySelector("[data-testid=marvel0-command]"),
    ).not.toBeNull();
  });

  test("the page says what its status cards and its action cards do, and that nothing is an emergency stop", async () => {
    core(healthy());
    const { host } = await render(<Marvel0Page />);
    await loaded(host);
    const text = host.textContent!;
    for (const key of [
      "marvel0.scope.read_only",
      "marvel0.scope.no_connect",
      "marvel0.scope.actions",
      "marvel0.scope.gates",
      "marvel0.scope.no_estop",
      "marvel0.deploy.scope",
      "marvel0.cmd.scope",
    ] as const)
      expect(text).toContain(en[key]);
    expect(en["marvel0.scope.read_only"]).toContain(
      "status cards are read-only",
    );
    // It says what the buttons do (the stack, the policy server, the client),
    // not that the page never starts them, and what the gates are worth.
    expect(en["marvel0.scope.actions"]).toContain("starts and stops");
    expect(en["marvel0.scope.actions"]).not.toMatch(/never start/i);
    expect(en["marvel0.scope.gates"]).toContain("do not stop another program");
  });

  test("a deploy goes through the page's own proxy route as one JSON POST with nothing but a content type", async () => {
    const calls = core(healthy(), {
      "/api/levi/marvel0/deploy": { status: 202, body: deployed() },
    });
    const { host } = await render(<Marvel0Page />);
    await loaded(host);
    const card = host.querySelector(
      "[data-testid=marvel0-deploy]",
    ) as HTMLElement;
    await click(button(card, "Deploy")!);
    const dialog = host.querySelector("[role=dialog]") as HTMLElement;
    // The window has nothing to type: the page sends the phrase itself.
    expect(dialog.querySelectorAll("input:not([type=checkbox])")).toHaveLength(
      0,
    );
    await click(button(dialog, "Deploy")!);
    await tick(3, 5);
    const posts = calls.filter((call) => call.init?.method === "POST");
    expect(posts).toHaveLength(1);
    expect(posts[0].path).toBe("/api/levi/marvel0/deploy");
    expect(
      [...new Headers(posts[0].init?.headers).keys()].map((name) =>
        name.toLowerCase(),
      ),
    ).toEqual(["content-type"]);
    expect(JSON.parse(String(posts[0].init?.body))).toMatchObject({
      model_id: "pi05_fr3_all_step49999",
      rtc: true,
      fraction: 0.22,
      confirm: "deploy pi05_fr3_all_step49999",
    });
  });

  test("the command card posts only to the preview route", async () => {
    const calls = core(healthy(ready()), {
      "/api/levi/marvel0/command/preview": { status: 200, body: preview() },
    });
    const { host } = await render(<Marvel0Page />);
    await loaded(host);
    const card = host.querySelector(
      "[data-testid=marvel0-command]",
    ) as HTMLElement;
    await click(button(card, "Make the command")!);
    await tick(3, 5);
    const posts = calls.filter((call) => call.init?.method === "POST");
    expect(posts.map((call) => call.path)).toEqual([
      "/api/levi/marvel0/command/preview",
    ]);
    expect(
      host.querySelector("[data-testid=marvel0-command-text]"),
    ).not.toBeNull();
  });

  test("in Chinese the action cards are in Chinese", async () => {
    localStorage.setItem("levi-language", "zh");
    core(healthy());
    const { host } = await render(
      <LocaleProvider>
        <Marvel0Page />
      </LocaleProvider>,
    );
    await waitFor(
      () =>
        host
          .querySelector("[data-testid=marvel0-actions]")
          ?.textContent?.includes(zh["marvel0.step.policy"]),
      { label: "the Chinese action section" },
    );
    const actions = host.querySelector("[data-testid=marvel0-actions]")!;
    expect(actions.textContent).toContain(zh["marvel0.deploy.scope"]);
    expect(actions.textContent).toContain(zh["marvel0.cmd.scope"]);
    expect(actions.textContent).not.toContain(en["marvel0.deploy.scope"]);
  });

  test("a core without the new routes leaves the status cards working and says the actions cannot be read", async () => {
    core({ [SETUP]: quietSetup(), [MARVEL0]: quietMarvel0() });
    const { host } = await render(<Marvel0Page />);
    await waitFor(() => host.querySelector("[data-testid=marvel0-idle]"), {
      label: "the status cards",
    });
    await waitFor(
      () =>
        host
          .querySelector("[data-testid=marvel0-deploy]")
          ?.textContent?.includes(en["marvel0.deploy.unreadable"]),
      { label: "the notice" },
    );
    const card = host.querySelector(
      "[data-testid=marvel0-deploy]",
    ) as HTMLElement;
    expect(button(card, "Deploy")!.disabled).toBe(true);
  });
});
