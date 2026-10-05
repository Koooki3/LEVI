import { flush, render, setupDom } from "@/components/ds/__tests__/dom";
import { afterEach, describe, expect, mock, test } from "bun:test";
import { LocaleProvider } from "@/components/levi-locale";

mock.module("next/navigation", () => ({
  useRouter: () => ({ push: () => {}, replace: () => {} }),
  usePathname: () => "/workbench",
}));

const { default: Workbench } = await import("../page");

setupDom();

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
  try {
    localStorage.clear();
  } catch {
    // no storage in this DOM
  }
});

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

function serve(answer: (path: string) => Response) {
  globalThis.fetch = mock((input: RequestInfo | URL) =>
    Promise.resolve(answer(String(input))),
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

describe("Conversion & review when LEVI's service does not answer", () => {
  test("says it cannot tell; it does not claim ffmpeg is missing or sync is off", async () => {
    serve(DOWN);
    const { host } = await render(<Workbench />);
    await flush(80);
    const text = host.textContent ?? "";
    expect(text).not.toContain("Install ffmpeg");
    expect(text).not.toContain("Auto-sync off");
    expect(text).not.toContain("Auto-sync paused");
    expect(text).toContain("Cannot tell right now");
    expect(text).toContain("Auto-sync: cannot tell right now");
    // Neither the empty-list state nor an empty table stands in for the list.
    expect(text).not.toContain("No local datasets registered yet.");
    expect(host.querySelector("table.ds-table")).toBeNull();
  });

  test("the error is the three-part box with Try again, and a reason that reads", async () => {
    serve(DOWN);
    const { host } = await render(<Workbench />);
    await flush(80);
    const box = host.querySelector(".pg-problem")!;
    expect(box.querySelector(".pg-problem__title")!.textContent).toBe(
      "The page could not read LEVI's state",
    );
    expect(box.querySelector(".pg-problem__why")!.textContent).toContain(
      "did not answer",
    );
    expect(box.querySelector(".pg-problem__fix code")!.textContent).toBe(
      "uv run levi serve",
    );
    expect(box.querySelector(".pg-problem__fix button")!.textContent).toBe(
      "Try again",
    );
    // The server's English sentence is not shown to a Chinese reader either:
    // here (English) it is replaced by the plain explanation too.
    expect(box.textContent).not.toContain("LEVI backend unavailable");
  });

  test("buttons that cannot work say why, next to them", async () => {
    serve(DOWN);
    const { host } = await render(<Workbench />);
    await flush(80);
    const inspect = [...host.querySelectorAll("button")].find((b) =>
      b.textContent?.includes("Inspect input"),
    )!;
    expect(inspect.disabled).toBe(true);
    const why = host.querySelector(
      `#${inspect.getAttribute("aria-describedby")}`,
    )!;
    expect(why.textContent).toContain("Try again above");
    expect(why.textContent).not.toContain("ffmpeg");
  });

  test("Chinese: nothing English is left in the error", async () => {
    localStorage.setItem("levi-language", "zh");
    serve(DOWN);
    const { host } = await render(
      <LocaleProvider>
        <Workbench />
      </LocaleProvider>,
    );
    await flush(120);
    const box = host.querySelector(".pg-problem")!;
    expect(box.textContent).toContain("页面读不到 LEVI 的状态");
    expect(box.textContent).toContain("没有回应");
    expect(box.textContent).not.toContain("backend unavailable");
    expect(host.textContent).not.toContain("请安装 ffmpeg");
    expect(host.textContent).not.toContain("自动同步已关闭");
    expect(host.textContent).toContain("暂时无法判断");
  });
});

describe("Conversion & review with a working service", () => {
  test("a missing ffmpeg is still reported as missing, with the reason beside the button", async () => {
    serve((path) =>
      path.includes("/convert/formats")
        ? json({ inputs: [], outputs: [], unsupported: [] })
        : path.includes("/catalog")
          ? json({
              local: [],
              workspace: "/w",
              conversion_available: false,
              stages: [],
            })
          : path.includes("/sync")
            ? json({
                enabled: true,
                running: true,
                interval_seconds: 5,
                last_scan: null,
                last_error: null,
                pending: [],
                changes: [],
              })
            : json([]),
    );
    const { host } = await render(<Workbench />);
    await flush(80);
    const text = host.textContent ?? "";
    expect(text).toContain("Install ffmpeg");
    expect(text).toContain("Auto-sync on");
    expect(text).toContain("Inspection needs ffmpeg");
    expect(host.querySelector(".pg-problem")).toBeNull();
  });
});
