import {
  click,
  render,
  setupDom,
  waitFor,
} from "@/components/ds/__tests__/dom";
import { afterEach, describe, expect, mock, test } from "bun:test";
import { LocaleProvider } from "@/components/levi-locale";

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
    const box = await waitFor(() => host.querySelector(".pg-problem"), {
      label: "the pool's read error",
    });
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
    const scan = await waitFor(
      () =>
        [...host.querySelectorAll("button")].find((b) =>
          b.textContent?.includes("Scan now"),
        ),
      { label: "the Scan now button" },
    );
    expect(host.querySelector(".pg-problem")).toBeNull();
    await click(scan);
    const box = await waitFor(() => host.querySelector(".pg-problem"), {
      label: "the scan error",
    });
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
    const exportButton = await waitFor(
      () =>
        [...host.querySelectorAll("button")].find((b) =>
          b.textContent?.includes("Export…"),
        ),
      { label: "the Export button" },
    );
    await waitFor(() => exportButton.getAttribute("aria-describedby"), {
      label: "the reason the Export button is off",
    });
    expect(exportButton.disabled).toBe(true);
    const why = host.querySelector(
      `#${exportButton.getAttribute("aria-describedby")}`,
    )!;
    expect(why.textContent).toContain("not enabled");
    expect(why.textContent).not.toContain("Scan the pool");
  });

  test("Chinese labels take the full-width colon; English keeps its own", async () => {
    const answer = (path: string) =>
      path.includes("/pool/status")
        ? json(STATUS)
        : path.includes("/pool/recipes")
          ? json({ recipes: [] })
          : path.includes("/pool/cleanup")
            ? json({
                partials: [],
                jobs: [],
                disk: [],
                freeable: 0,
              })
            : json({});
    localStorage.setItem("levi-language", "zh");
    serve(answer);
    const zh = await render(
      <LocaleProvider>
        <PoolPage />
      </LocaleProvider>,
    );
    await waitFor(() => zh.host.textContent?.includes("上次扫描："), {
      label: "the Chinese scan label",
    });
    expect(zh.host.textContent).toContain("上次扫描：从未");
    expect(zh.host.textContent).not.toContain("上次扫描:");
    localStorage.clear();
    serve(answer);
    const en = await render(<PoolPage />);
    await waitFor(() => en.host.textContent?.includes("Last scan"), {
      label: "the English scan label",
    });
    expect(en.host.textContent).toContain("Last scan: never");
  });
});
