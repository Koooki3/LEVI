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
import { quietRig, quietSetup } from "./fixtures";
import { button, field, tick, type } from "./actions-helpers";

const { default: RigPage } = await import("@/app/rig/page");
setupDom();

const SETUP = "/api/levi/setup/status";
const RIG = "/api/levi/rig/status";
const READS = [
  SETUP,
  RIG,
  "/api/levi/rig/models",
  "/api/levi/rig/site",
  "/api/levi/rig/deploy",
  "/api/levi/rig/deploy/log?lines=80",
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
  [RIG]: quietRig(),
  "/api/levi/rig/models": modelsAnswer(),
  "/api/levi/rig/site": siteAnswer(),
  "/api/levi/rig/deploy": deploy,
  "/api/levi/rig/deploy/log?lines=80": logAnswer(),
});
const loaded = (host: HTMLElement) =>
  waitFor(() => host.querySelector("[data-testid=rig-deploy][data-state]"), {
    label: "the policy server card",
  });

describe("the Rig console with its action cards", () => {
  test("opening the page only reads: every request is a plain GET, and nothing is written", async () => {
    const calls = core(healthy());
    const { host } = await render(<RigPage />);
    await loaded(host);
    await tick(3, 5);
    expect(new Set(calls.map((call) => call.path))).toEqual(
      new Set(READS.slice(0, 5)),
    );
    for (const call of calls) {
      expect(call.init?.method ?? "GET").toBe("GET");
      expect(call.init?.body).toBeUndefined();
      expect(call.init?.cache).toBe("no-store");
    }
  });

  test("the status cards are still there, and the action section follows them", async () => {
    core(healthy());
    const { host } = await render(<RigPage />);
    await loaded(host);
    expect(host.querySelector("[data-testid=rig-idle]")).not.toBeNull();
    expect(host.querySelector("[data-testid=rig-card-policy]")).not.toBeNull();
    const actions = host.querySelector("[data-testid=rig-actions]")!;
    expect(
      actions.compareDocumentPosition(
        host.querySelector("[data-testid=rig-card-gpu]")!,
      ) & Node.DOCUMENT_POSITION_PRECEDING,
    ).toBeTruthy();
    expect(actions.querySelector("[data-testid=rig-deploy]")).not.toBeNull();
    expect(actions.querySelector("[data-testid=rig-command]")).not.toBeNull();
  });

  test("the page says what its status cards and its action cards do, and that nothing is an emergency stop", async () => {
    core(healthy());
    const { host } = await render(<RigPage />);
    await loaded(host);
    const text = host.textContent!;
    for (const key of [
      "rig.scope.read_only",
      "rig.scope.no_connect",
      "rig.scope.actions",
      "rig.scope.no_estop",
      "rig.act.scope",
      "rig.deploy.scope",
      "rig.cmd.scope",
    ] as const)
      expect(text).toContain(en[key]);
    expect(en["rig.scope.read_only"]).toContain("status cards are read-only");
    expect(en["rig.scope.actions"]).toContain("never start the arm stack");
  });

  test("a deploy goes through the page's own proxy route as one JSON POST with nothing but a content type", async () => {
    const calls = core(healthy(), {
      "/api/levi/rig/deploy": { status: 202, body: deployed() },
    });
    const { host } = await render(<RigPage />);
    await loaded(host);
    const card = host.querySelector("[data-testid=rig-deploy]") as HTMLElement;
    await click(button(card, "Deploy")!);
    const dialog = host.querySelector("[role=dialog]") as HTMLElement;
    await type(
      field(dialog, "Confirmation phrase"),
      "deploy pi05_fr3_all_step49999",
    );
    await click(button(dialog, "Deploy")!);
    await tick(3, 5);
    const posts = calls.filter((call) => call.init?.method === "POST");
    expect(posts).toHaveLength(1);
    expect(posts[0].path).toBe("/api/levi/rig/deploy");
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
      "/api/levi/rig/command/preview": { status: 200, body: preview() },
    });
    const { host } = await render(<RigPage />);
    await loaded(host);
    const card = host.querySelector("[data-testid=rig-command]") as HTMLElement;
    await click(button(card, "Make the command")!);
    await tick(3, 5);
    const posts = calls.filter((call) => call.init?.method === "POST");
    expect(posts.map((call) => call.path)).toEqual([
      "/api/levi/rig/command/preview",
    ]);
    expect(host.querySelector("[data-testid=rig-command-text]")).not.toBeNull();
  });

  test("in Chinese the action cards are in Chinese", async () => {
    localStorage.setItem("levi-language", "zh");
    core(healthy());
    const { host } = await render(
      <LocaleProvider>
        <RigPage />
      </LocaleProvider>,
    );
    await waitFor(
      () =>
        host
          .querySelector("[data-testid=rig-actions]")
          ?.textContent?.includes(zh["rig.act.title"]),
      { label: "the Chinese action section" },
    );
    const actions = host.querySelector("[data-testid=rig-actions]")!;
    expect(actions.textContent).toContain(zh["rig.deploy.scope"]);
    expect(actions.textContent).toContain(zh["rig.cmd.scope"]);
    expect(actions.textContent).not.toContain(en["rig.deploy.scope"]);
  });

  test("a core without the new routes leaves the status cards working and says the actions cannot be read", async () => {
    core({ [SETUP]: quietSetup(), [RIG]: quietRig() });
    const { host } = await render(<RigPage />);
    await waitFor(() => host.querySelector("[data-testid=rig-idle]"), {
      label: "the status cards",
    });
    await waitFor(
      () =>
        host
          .querySelector("[data-testid=rig-deploy]")
          ?.textContent?.includes(en["rig.deploy.unreadable"]),
      { label: "the notice" },
    );
    const card = host.querySelector("[data-testid=rig-deploy]") as HTMLElement;
    expect(button(card, "Deploy")!.disabled).toBe(true);
  });
});
