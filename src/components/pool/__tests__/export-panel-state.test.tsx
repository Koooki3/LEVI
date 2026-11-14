import { act, useState } from "react";
import { click, fire, render, setupDom, waitFor } from "../../ds/__tests__/dom";
import { afterEach, describe, expect, mock, test } from "bun:test";
import { ExportPanel } from "../export-panel";
import type { ExportFormat, Preview, Recipe } from "../types";

setupDom();

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});

const RECIPE: Recipe = {
  name: "mix",
  categories: [],
  sources: [],
  tasks: [{ task: "a", count: null, success_ratio: null, strategy: "quality" }],
  outcome: "all",
  per_task_cap: null,
  seed: 0,
  date_from: null,
  date_to: null,
  policies: [],
  exclude: [],
  include_nonstandard: false,
};
const PREVIEW = {
  episodes: 120,
  frames: 1000,
  excluded_heldout: 0,
} as unknown as Preview;

const bodies: unknown[] = [];
function serve(delayDry = 0) {
  bodies.length = 0;
  globalThis.fetch = mock(
    async (_input: RequestInfo | URL, init?: RequestInit) => {
      bodies.push(JSON.parse(String(init?.body ?? "{}")));
      if (delayDry) await new Promise((r) => setTimeout(r, delayDry));
      return new Response(
        JSON.stringify({
          id: "plan",
          kind: "export",
          status: "planned",
          planned_episodes: 120,
          planned_excluded: 0,
          target: "/exports/pi05",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    },
  ) as unknown as typeof fetch;
}

function Harness({
  previewing = false,
  fps: startFps = 10,
}: {
  previewing?: boolean;
  fps?: number;
}) {
  const [format, setFormat] = useState<ExportFormat>("lerobot_v21");
  const [fps, setFps] = useState(startFps);
  return (
    <ExportPanel
      recipe={RECIPE}
      preview={PREVIEW}
      previewing={previewing}
      exportRoots={["/exports"]}
      job={null}
      humanAsSuccess={false}
      onHumanAsSuccess={() => {}}
      format={format}
      onFormat={setFormat}
      fps={fps}
      onFps={setFps}
      timing={null}
      onTiming={() => {}}
      onJob={() => {}}
      onPush={() => {}}
    />
  );
}

// This DOM reads a controlled field's value on keyup (as in shell-dom.test).
async function typeInto(input: HTMLInputElement, value: string) {
  await act(async () => input.focus());
  await act(async () => {
    Object.getOwnPropertyDescriptor(
      HTMLInputElement.prototype,
      "value",
    )!.set!.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(
      new KeyboardEvent("keyup", { bubbles: true, key: "e" }),
    );
  });
}

const button = (host: Element, label: string) =>
  [...host.querySelectorAll("button")].find((b) =>
    b.textContent?.includes(label),
  ) as HTMLButtonElement;

describe("the dry-run plan", () => {
  test("stays only while the request it answered is what the form says", async () => {
    serve();
    const { host } = await render(<Harness />);
    const name = host.querySelector<HTMLInputElement>("#pool-export-name")!;
    await typeInto(name, "pi05");
    await click(button(host, "Dry run"));
    await waitFor(() => host.querySelector(".pg-pool-plan"), {
      label: "the plan",
    });
    expect(host.querySelector(".pg-pool-plan")!.textContent).toContain("120");
    // A new name: the plan is no longer shown, and the page says why.
    await typeInto(name, "pi05b");
    expect(host.querySelector(".pg-pool-plan")).toBeNull();
    expect(host.textContent).toContain("no longer shown");
    // The same name again: it answers the form once more.
    await typeInto(name, "pi05");
    expect(host.querySelector(".pg-pool-plan")).not.toBeNull();
    // A different format.
    const format = host.querySelector("select")!;
    format.value = "recap_value";
    await fire(format, new Event("change", { bubbles: true }));
    expect(host.querySelector(".pg-pool-plan")).toBeNull();
  });
});

describe("while the preview is recomputing", () => {
  test("the numbers are dimmed, Updating… is said, and both buttons say why they are off", async () => {
    serve();
    const { host } = await render(<Harness previewing />);
    await typeInto(
      host.querySelector<HTMLInputElement>("#pool-export-name")!,
      "pi05",
    );
    expect(host.querySelector(".pg-pool-updating")).not.toBeNull();
    expect(host.textContent).toContain("Updating…");
    const start = button(host, "Start export");
    expect(start.disabled).toBe(true);
    expect(button(host, "Dry run").disabled).toBe(true);
    const why = host.querySelector(
      `#${start.getAttribute("aria-describedby")}`,
    )!;
    expect(why.textContent).toContain("Waiting for the preview");
  });

  test("not while it is settled", async () => {
    serve();
    const { host } = await render(<Harness />);
    await typeInto(
      host.querySelector<HTMLInputElement>("#pool-export-name")!,
      "pi05",
    );
    expect(host.querySelector(".pg-pool-updating")).toBeNull();
    expect(button(host, "Start export").disabled).toBe(false);
  });
});

describe("which button waits", () => {
  test("a dry run spins only the Dry run button", async () => {
    serve(150);
    const { host } = await render(<Harness />);
    await typeInto(
      host.querySelector<HTMLInputElement>("#pool-export-name")!,
      "pi05",
    );
    await click(button(host, "Dry run"));
    expect(button(host, "Dry run").getAttribute("aria-busy")).toBe("true");
    expect(button(host, "Start export").getAttribute("aria-busy")).toBeNull();
    await waitFor(() => host.querySelector(".pg-pool-plan"), {
      label: "the plan",
    });
    expect(button(host, "Dry run").getAttribute("aria-busy")).toBeNull();
  });
});

describe("the FPS field", () => {
  const fpsInput = (host: Element) =>
    host.querySelector<HTMLInputElement>("input[type=number][max='240']")!;

  test("deleting the digits does not snap back to 10; the reason is said and Start is off", async () => {
    serve();
    const { host } = await render(<Harness fps={30} />);
    await typeInto(
      host.querySelector<HTMLInputElement>("#pool-export-name")!,
      "pi05",
    );
    expect(fpsInput(host).value).toBe("30");
    await typeInto(fpsInput(host), "");
    expect(fpsInput(host).value).toBe("");
    expect(fpsInput(host).getAttribute("aria-invalid")).toBe("true");
    expect(host.textContent).toContain("Enter a whole number from 1 to 240.");
    expect(button(host, "Start export").disabled).toBe(true);
    expect(button(host, "Dry run").disabled).toBe(true);
  });

  test("a valid number is the one that is sent; past the range it is clamped to 240", async () => {
    serve();
    const { host } = await render(<Harness fps={30} />);
    await typeInto(
      host.querySelector<HTMLInputElement>("#pool-export-name")!,
      "pi05",
    );
    await typeInto(fpsInput(host), "");
    await typeInto(fpsInput(host), "25");
    await click(button(host, "Dry run"));
    await waitFor(() => bodies.length === 1, { label: "the request" });
    expect((bodies[0] as { options: { fps: number } }).options.fps).toBe(25);
    await typeInto(fpsInput(host), "999");
    expect(fpsInput(host).value).toBe("240");
  });
});
