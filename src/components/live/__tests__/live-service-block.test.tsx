import {
  click,
  render,
  setupDom,
  waitFor,
} from "@/components/ds/__tests__/dom";
import { afterEach, describe, expect, test } from "bun:test";
import { act } from "react";
import { LiveServiceBlock } from "../live-service-block";
import type { ServiceOperation, ServiceStatus } from "../live-service-logic";

setupDom();

const confirm = {
  start: "start-live",
  stop: "stop-live",
  force_phrase: "stop live during evaluation",
};
const stopped: ServiceStatus = {
  enabled: true,
  holder: { alive: false },
  unit: { name: "levi-live.service", installed: true, state: "inactive" },
  ports: [
    { port: 7881, role: "core", state: "free" },
    { port: 8100, role: "vllm", state: "foreign", pid: 99, name: "python" },
  ],
  gpu: {
    state: "ok",
    others: [],
    lock: { state: "free" },
    vllm_state: "stopped",
  },
  evaluation: { active: false },
  start_checks: [],
  stop_checks: [],
  needs_confirmation: { start: [], stop: [] },
  operation: null,
  confirm,
};
const runningByUnit: ServiceStatus = {
  ...stopped,
  holder: { alive: true, pid: 4242, started_by: "unit" },
  unit: { name: "levi-live.service", installed: true, state: "active" },
};

type Call = { method: string; path: string; body?: Record<string, unknown> };
let calls: Call[] = [];
const original = globalThis.fetch;

/** A core that answers `routes` (path after /api/levi/live/ -> response). */
function core(routes: Record<string, () => Response | Promise<Response>>) {
  calls = [];
  globalThis.fetch = (async (url: string, init?: RequestInit) => {
    const path = String(url).replace("/api/levi/live/", "");
    calls.push({
      method: init?.method ?? "GET",
      path,
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
    });
    const route = routes[`${init?.method ?? "GET"} ${path}`];
    if (!route) return new Response("{}", { status: 404 });
    return route();
  }) as unknown as typeof fetch;
}
afterEach(() => {
  globalThis.fetch = original;
});
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
const op = (extra: Partial<ServiceOperation> = {}): ServiceOperation => ({
  operation_id: "op-1",
  action: "start",
  request_id: "r",
  state: "running",
  ...extra,
});
const button = (host: HTMLElement, text: string) =>
  Array.from(host.querySelectorAll("button")).find((node) =>
    node.textContent?.includes(text),
  ) as HTMLButtonElement | undefined;

async function type(field: HTMLInputElement, text: string) {
  // React reads the value from the focused field on keyup (as in shell-dom).
  await act(async () => field.focus());
  await act(async () => {
    Object.getOwnPropertyDescriptor(
      HTMLInputElement.prototype,
      "value",
    )!.set!.call(field, text);
    field.dispatchEvent(new Event("input", { bubbles: true }));
    field.dispatchEvent(
      new KeyboardEvent("keyup", { bubbles: true, key: "e" }),
    );
  });
}
const inputs = (host: HTMLElement) =>
  Array.from(
    host.querySelectorAll("input[type=text], input:not([type])"),
  ) as HTMLInputElement[];

async function show(
  status: ServiceStatus,
  extra: Parameters<typeof core>[0] = {},
) {
  core({ "GET service": () => json(status), ...extra });
  const view = await render(<LiveServiceBlock />);
  await waitFor(() => view.host.querySelector("[data-kind]"), {
    label: "the block",
  });
  return view;
}

describe("the status of the service", () => {
  test("shows the state, who started it, the ports and the GPU facts in words", async () => {
    const { host } = await show({
      ...runningByUnit,
      holder: { alive: true, pid: 4242, started_by: "terminal" },
    });
    const text = host.textContent ?? "";
    expect(text).toContain("Running");
    expect(text).toContain("Started from a terminal (pid 4242)");
    expect(text).toContain("7881");
    expect(text).toContain("Held by someone else");
    expect(text).toContain("pid 99 (python)");
    // A service started from a terminal cannot be started again.
    expect(button(host, "Start the live service")).toBeUndefined();
  });

  test("lists the reasons a start is refused as text", async () => {
    const { host } = await show({
      ...stopped,
      start_checks: [
        {
          code: "port_foreign",
          level: "refuse",
          message: "Port 8100 (vllm) is held by pid 99",
        },
      ],
    });
    expect(host.textContent).toContain("A start is refused for now:");
    expect(host.textContent).toContain("A port is held by another process.");
    expect(host.textContent).toContain("Port 8100 (vllm) is held by pid 99");
    expect(button(host, "Start the live service")!.disabled).toBe(true);
    expect(host.textContent).toContain("The checks above refuse it.");
  });

  test("a switched-off control says so in words and offers no button", async () => {
    const { host } = await show({ ...stopped, enabled: false });
    expect(host.textContent).toContain(
      "Starting and stopping from this page is not enabled.",
    );
    expect(host.textContent).toContain("LEVI_LIVE_SERVICE_CONTROL=1");
    expect(button(host, "Start the live service")).toBeUndefined();
    expect(button(host, "Stop the live service")).toBeUndefined();
    // The terminal command stays as the fallback.
    expect(host.textContent).toContain("levi live start");
  });
});

describe("starting", () => {
  const gpuStatus: ServiceStatus = {
    ...stopped,
    gpu: {
      ...stopped.gpu,
      others: [{ pid: 7, name: "train.py", memory_mib: 20480 }],
    },
    needs_confirmation: { start: ["gpu_shared"], stop: [] },
  };

  test("needs the GPU tick and the phrase, then sends one request with a request id", async () => {
    let release: (value: Response) => void = () => undefined;
    const { host } = await show(gpuStatus, {
      "POST service/start": () =>
        new Promise<Response>((resolve) => (release = resolve)),
      "GET service/operations/op-1": () => json(op({ state: "done" })),
    });
    expect(host.textContent).toContain("train.py (pid 7, 20.0 GiB)");
    const start = () => button(host, "Start the live service")!;
    expect(start().disabled).toBe(true);
    expect(host.textContent).toContain("Tick the GPU confirmation first.");
    await type(inputs(host)[0], "start-live");
    expect(start().disabled).toBe(true);
    expect(host.textContent).toContain("Tick the GPU confirmation first.");
    await click(host.querySelector("input[type=checkbox]"));
    expect(start().disabled).toBe(false);

    // Two presses before the answer: one request.
    await click(start());
    await click(button(host, "Start the live service"));
    const posts = calls.filter((c) => c.method === "POST");
    expect(posts).toHaveLength(1);
    expect(posts[0].body).toMatchObject({
      confirm: "start-live",
      confirm_gpu_shared: true,
      reset_failed: false,
    });
    expect(String(posts[0].body!.request_id).length).toBeGreaterThanOrEqual(8);
    await act(async () => release(json(op(), 202)));
    await waitFor(
      () => host.textContent?.includes("Start of the live service"),
      {
        label: "the operation",
      },
    );
    expect(host.textContent).toContain("In progress");
  });

  test("a failed unit is reset and started by one labelled button", async () => {
    const { host } = await show({
      ...stopped,
      unit: { name: "levi-live.service", installed: true, state: "failed" },
      needs_confirmation: { start: ["unit_failed"], stop: [] },
    });
    await type(inputs(host)[0], "start-live");
    const reset = button(host, "Reset the failed state and start")!;
    expect(reset.disabled).toBe(false);
    core({
      "GET service": () => json(stopped),
      "POST service/start": () => json(op(), 202),
    });
    await click(reset);
    expect(calls.find((c) => c.method === "POST")!.body).toMatchObject({
      reset_failed: true,
    });
  });

  test("423 shows the operation that is already running", async () => {
    const { host } = await show(stopped, {
      "POST service/start": () =>
        json(
          {
            detail: {
              code: "busy",
              message:
                "Another start or stop of the live service is in progress",
              operation_id: "op-9",
            },
          },
          423,
        ),
      "GET service/operations/op-9": () => json(op({ operation_id: "op-9" })),
    });
    await type(inputs(host)[0], "start-live");
    await click(button(host, "Start the live service"));
    await waitFor(() => host.textContent?.includes("In progress"), {
      label: "the running operation",
    });
    expect(host.textContent).toContain("Start or stop of the live service");
  });

  test("403 disabled from the core turns the block read-only", async () => {
    const { host } = await show(stopped, {
      "POST service/start": () =>
        json(
          {
            detail: {
              code: "disabled",
              message: "Starting and stopping is off",
            },
          },
          403,
        ),
    });
    await type(inputs(host)[0], "start-live");
    await click(button(host, "Start the live service"));
    await waitFor(
      () => host.textContent?.includes("LEVI_LIVE_SERVICE_CONTROL=1"),
      { label: "the disabled note" },
    );
    expect(button(host, "Start the live service")).toBeUndefined();
  });

  test("a refusal is shown and the next press is a new request", async () => {
    const seen: string[] = [];
    const { host } = await show(stopped, {
      "POST service/start": () => {
        seen.push(String(calls.at(-1)!.body!.request_id));
        return json(
          { detail: { code: "port_foreign", message: "Port 8100 is held" } },
          409,
        );
      },
    });
    await type(inputs(host)[0], "start-live");
    await click(button(host, "Start the live service"));
    await waitFor(
      () => host.textContent?.includes("The request was not carried out"),
      {
        label: "the refusal",
      },
    );
    await click(button(host, "Start the live service"));
    expect(seen).toHaveLength(2);
    expect(seen[0]).not.toBe(seen[1]);
  });
});

describe("stopping", () => {
  test("is asked for first; an evaluation in progress needs the long phrase", async () => {
    const { host } = await show({
      ...runningByUnit,
      evaluation: { active: true, count: 1 },
      stop_checks: [
        {
          code: "evaluation_active",
          level: "confirm",
          message: "An evaluation session is running",
        },
      ],
      needs_confirmation: { start: [], stop: ["evaluation_active"] },
    });
    expect(host.textContent).toContain("A session is running");
    await click(button(host, "Stop the live service…"));
    expect(host.textContent).toContain("Stopping ends the online judgement");
    const stop = () => button(host, "Stop during the evaluation")!;
    expect(stop().disabled).toBe(true);
    const [stopField, forceField] = inputs(host);
    await type(stopField, "stop-live");
    expect(stop().disabled).toBe(true);
    expect(host.textContent).toContain(
      "Type the phrase for stopping during an evaluation.",
    );
    await type(forceField, "stop live during evaluation");
    expect(stop().disabled).toBe(false);
    core({
      "GET service": () => json(runningByUnit),
      "POST service/stop": () => json(op({ action: "stop" }), 202),
    });
    await click(stop());
    const post = calls.find((c) => c.method === "POST")!;
    expect(post.path).toBe("service/stop");
    expect(post.body).toMatchObject({
      confirm: "stop-live",
      force_phrase: "stop live during evaluation",
    });
  });

  test("without an evaluation only the short phrase is asked for", async () => {
    const { host } = await show(runningByUnit);
    await click(button(host, "Stop the live service…"));
    expect(inputs(host)).toHaveLength(1);
    await type(inputs(host)[0], "stop-live");
    expect(button(host, "Stop the live service")!.disabled).toBe(false);
  });

  test("the page of the live service itself offers no button", async () => {
    const { host } = await show({
      ...runningByUnit,
      served_by_live_service: true,
    });
    expect(host.textContent).toContain("served by the live service itself");
    expect(button(host, "Stop the live service…")).toBeUndefined();
  });
});
