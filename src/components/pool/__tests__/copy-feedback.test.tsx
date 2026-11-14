import {
  click,
  flush,
  render,
  setupDom,
  waitFor,
} from "../../ds/__tests__/dom";
import { afterEach, describe, expect, mock, test } from "bun:test";

mock.module("next/navigation", () => ({
  useRouter: () => ({ push: () => {}, replace: () => {} }),
  usePathname: () => "/pool",
}));

const { JobBanner } = await import("../job-panel");
type PoolJob = import("../types").PoolJob;

setupDom();

const realFetch = globalThis.fetch;
const realClipboard = Object.getOwnPropertyDescriptor(navigator, "clipboard");
afterEach(() => {
  globalThis.fetch = realFetch;
  if (realClipboard)
    Object.defineProperty(navigator, "clipboard", realClipboard);
  else delete (navigator as unknown as Record<string, unknown>).clipboard;
});

const failed = { id: "export-1", kind: "export", status: "failed" } as PoolJob;
const copyButton = (host: Element) =>
  [...host.querySelectorAll("button")].find((b) =>
    /^Cop(y error report|ied|y failed)$/.test(b.textContent ?? ""),
  )!;

function setup(write: () => Promise<void> | undefined) {
  globalThis.fetch = mock(
    async () =>
      new Response(JSON.stringify({ a: 1 }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
  ) as unknown as typeof fetch;
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText: write },
  });
}

describe("copying the error report", () => {
  test("a refused clipboard says so, then the button is itself again", async () => {
    setup(() => Promise.reject(new Error("denied")));
    const { host } = await render(
      <JobBanner job={failed} onJob={() => {}} onLog={() => {}} />,
    );
    await click(copyButton(host));
    await waitFor(() => copyButton(host).textContent === "Copy failed", {
      label: "the failure on the button",
    });
    expect(host.textContent).toContain(
      "The browser did not allow copying to the clipboard.",
    );
    await flush(2100);
    expect(copyButton(host).textContent).toBe("Copy error report");
  }, 10000);

  test("Copied goes back to Copy error report after two seconds", async () => {
    setup(() => Promise.resolve());
    const { host } = await render(
      <JobBanner job={failed} onJob={() => {}} onLog={() => {}} />,
    );
    await click(copyButton(host));
    await waitFor(() => copyButton(host).textContent === "Copied", {
      label: "Copied",
    });
    await flush(2100);
    expect(copyButton(host).textContent).toBe("Copy error report");
  }, 10000);
});
