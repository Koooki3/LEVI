import { click, flush, render, setupDom } from "@/components/ds/__tests__/dom";
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

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

const STATUS = {
  enabled: true,
  roots: ["/pool"],
  export_roots: ["/exports"],
  heldout_lists: [],
  heldout_disabled: true,
  warnings: [],
  disk: [],
  jobs: [],
  last_scan: null,
};

function serve(answer: (path: string, method: string) => Response) {
  globalThis.fetch = mock((input: RequestInfo | URL, init?: RequestInit) =>
    Promise.resolve(answer(String(input), init?.method ?? "GET")),
  ) as unknown as typeof fetch;
}

const DOWN = () =>
  json(
    {
      detail:
        "LEVI backend unavailable. Start both services with uv run levi serve.",
    },
    502,
  );

describe("the training pool's errors", () => {
  test("a failed read of the pool is a standing error: shown, not announced", async () => {
    serve(DOWN);
    const { host } = await render(<PoolPage />);
    await flush(80);
    const box = host.querySelector(".pg-problem")!;
    expect(box.querySelector(".pg-problem__title")!.textContent).toBe(
      "The training pool request failed",
    );
    expect(box.hasAttribute("role")).toBe(false);
    expect(box.querySelector(".pg-problem__fix button")!.textContent).toBe(
      "Try again",
    );
  });

  test("a failed action (Scan now) is announced to a screen reader", async () => {
    serve((path, method) =>
      method === "POST" && path.includes("/pool/scan")
        ? json({ detail: "A scan is already running" }, 409)
        : path.includes("/pool/status")
          ? json(STATUS)
          : path.includes("/pool/recipes")
            ? json({ recipes: [] })
            : path.includes("/pool/cleanup")
              ? json({ partials: [], jobs: [], disk: [], freeable: 0 })
              : json({}),
    );
    const { host } = await render(<PoolPage />);
    await flush(80);
    expect(host.querySelector(".pg-problem")).toBeNull();
    const scan = [...host.querySelectorAll("button")].find((b) =>
      b.textContent?.includes("Scan now"),
    )!;
    await click(scan);
    await flush(80);
    const box = host.querySelector(".pg-problem")!;
    expect(box.getAttribute("role")).toBe("alert");
    expect(box.textContent).toContain("A scan is already running");
  });

  test("the Export button says why it is off, and does not tell a disabled pool to scan", async () => {
    serve((path) =>
      path.includes("/pool/status")
        ? json({ ...STATUS, enabled: false, roots: [] })
        : path.includes("/pool/recipes")
          ? json({ recipes: [] })
          : json({}),
    );
    const { host } = await render(<PoolPage />);
    await flush(80);
    const exportButton = [...host.querySelectorAll("button")].find((b) =>
      b.textContent?.includes("Export…"),
    )!;
    expect(exportButton.disabled).toBe(true);
    const why = host.querySelector(
      `#${exportButton.getAttribute("aria-describedby")}`,
    )!;
    expect(why.textContent).toContain("not enabled");
    expect(why.textContent).not.toContain("Scan the pool");
  });
});
