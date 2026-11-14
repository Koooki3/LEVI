import {
  click,
  render,
  setupDom,
  waitFor,
} from "@/components/ds/__tests__/dom";
import { afterEach, describe, expect, mock, test } from "bun:test";

mock.module("next/navigation", () => ({
  useRouter: () => ({ push: () => {}, replace: () => {} }),
  usePathname: () => "/pool",
}));

const { default: PoolPage } = await import("../page");

setupDom();

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});

const json = (body: unknown) =>
  new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });

describe("Scan now", () => {
  test("a second click while the first request is on its way starts no second scan", async () => {
    let posts = 0;
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => (release = r));
    globalThis.fetch = mock(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = String(input);
        if ((init?.method ?? "GET") === "POST" && path.includes("/pool/scan")) {
          posts += 1;
          await gate;
          return json({ id: "scan-1", kind: "scan", status: "running" });
        }
        if (path.includes("/pool/status"))
          return json({
            enabled: true,
            roots: ["/pool"],
            export_roots: [],
            heldout_lists: [],
            warnings: [],
            disk: [],
            jobs: [],
            last_scan: null,
          });
        if (path.includes("/pool/recipes")) return json({ recipes: [] });
        if (path.includes("/pool/cleanup"))
          return json({
            partials: [],
            jobs: [],
            disk: [],
            reclaimable_bytes: 0,
          });
        return json({});
      },
    ) as unknown as typeof fetch;
    const { host } = await render(<PoolPage />);
    const scan = () =>
      [...host.querySelectorAll("button")].find((b) =>
        /Scan now|Scanning…/.test(b.textContent ?? ""),
      ) as HTMLButtonElement | undefined;
    await waitFor(
      () => scan()?.textContent === "Scan now" && !scan()!.disabled,
      {
        label: "Scan now enabled",
      },
    );
    const button = scan()!;
    await click(button);
    await click(button);
    await click(button);
    expect(posts).toBe(1);
    expect(button.getAttribute("aria-busy")).toBe("true");
    release();
    await waitFor(() => posts === 1, { label: "settled" });
  });
});
