import { click, flush, render, setupDom } from "@/components/ds/__tests__/dom";
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

const CATALOG = {
  local: [],
  workspace: "/w",
  conversion_available: true,
  stages: [],
};
const SYNC = {
  enabled: true,
  running: true,
  interval_seconds: 5,
  last_scan: null,
  last_error: null,
  pending: [],
  changes: [],
};
const FORMATS = { inputs: [], outputs: [], unsupported: [] };

/** A service that answers while `up.value` is true and, for a POST, with
 * `post` (default: an empty object). */
function flaky(up: { value: boolean }, post?: () => Response) {
  globalThis.fetch = mock((input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    if (!up.value && !(init?.method === "POST" && post))
      return Promise.resolve(DOWN());
    if (init?.method === "POST")
      return Promise.resolve((post ?? (() => json({})))());
    return Promise.resolve(
      path.includes("/convert/formats")
        ? json(FORMATS)
        : path.includes("/catalog")
          ? json(CATALOG)
          : path.includes("/sync")
            ? json(SYNC)
            : json([]),
    );
  }) as unknown as typeof fetch;
}

const syncNow = (host: HTMLElement) =>
  [...host.querySelectorAll("button")].find((b) =>
    b.textContent?.includes("Sync now"),
  )!;

describe("Conversion & review keeps what it knows honest", () => {
  test("an action that worked is not reported as failed because the re-read failed", async () => {
    const up = { value: true };
    // The sync request itself is answered; the reads after it are not.
    flaky(up, () => {
      up.value = false;
      return json({});
    });
    const { host } = await render(<Workbench />);
    await flush(80);
    expect(host.querySelector(".pg-problem")).toBeNull();
    await click(syncNow(host));
    await flush(120);
    const titles = [...host.querySelectorAll(".pg-problem__title")].map(
      (e) => e.textContent,
    );
    expect(titles).not.toContain("The sync did not run");
    expect(titles).toContain("The page could not read LEVI's state");
  });

  test("a service that stops while the page is open turns the badges to 'cannot tell'", async () => {
    const up = { value: true };
    // The service answers the sync request and is gone for the re-read, as
    // when it stops while the page is open and the next poll fails.
    flaky(up, () => {
      up.value = false;
      return json({});
    });
    const { host } = await render(<Workbench />);
    await flush(80);
    expect(host.textContent).toContain("Built in");
    expect(host.textContent).toContain("Auto-sync on");
    await click(syncNow(host));
    await flush(120);
    const text = host.textContent ?? "";
    expect(text).not.toContain("Built in");
    expect(text).not.toContain("Auto-sync on");
    expect(text).toContain("Cannot tell right now");
    expect(text).toContain("Auto-sync: cannot tell right now");
    expect(syncNow(host).disabled).toBe(true);
    expect(
      host.querySelector(`#${syncNow(host).getAttribute("aria-describedby")}`)!
        .textContent,
    ).toContain("Sync is off");
    const inspect = [...host.querySelectorAll("button")].find((b) =>
      b.textContent?.includes("Inspect input"),
    )!;
    expect(inspect.disabled).toBe(true);
  });

  test("a reply that is not JSON is not blamed on the conversion options", async () => {
    const up = { value: true };
    flaky(up, () => new Response("<html>proxy</html>", { status: 200 }));
    const { host } = await render(<Workbench />);
    await flush(80);
    await click(syncNow(host));
    await flush(80);
    const box = host.querySelector(".pg-problem")!;
    expect(box.querySelector(".pg-problem__title")!.textContent).toBe(
      "The sync did not run",
    );
    expect(box.textContent).not.toContain("not valid JSON");
  });
});

describe("Recent workspace changes", () => {
  const KINDS: [string, string, string][] = [
    ["added", "Added", "新增"],
    ["removed", "Removed", "移除"],
    ["updated", "Updated", "更新"],
    ["rebuilding", "Rebuilding view", "重建视图"],
    ["failed", "Failed", "失败"],
    ["skipped", "Skipped", "跳过"],
  ];
  const withChanges = () =>
    serve((path) =>
      path.includes("/convert/formats")
        ? json(FORMATS)
        : path.includes("/catalog")
          ? json(CATALOG)
          : path.includes("/sync")
            ? json({
                ...SYNC,
                changes: KINDS.map(([kind], i) => ({
                  time: 1_700_000_000 + i,
                  kind,
                  name: `ds-${kind}`,
                  detail: "",
                })),
              })
            : json([]),
    );

  test("each kind is a word, never the catalogue key", async () => {
    withChanges();
    const { host } = await render(<Workbench />);
    await flush(80);
    const items = [...host.querySelectorAll(".pg-sync-changes li")];
    expect(items).toHaveLength(KINDS.length);
    for (const [kind, english] of KINDS) {
      const item = items.find((li) => li.textContent?.includes(`ds-${kind}`))!;
      expect(item.querySelector("strong")!.textContent).toBe(english);
    }
    expect(host.textContent).not.toContain("sync.");
    expect(host.textContent).not.toContain("syncNow.");
  });

  test("Chinese too", async () => {
    localStorage.setItem("levi-language", "zh");
    withChanges();
    const { host } = await render(
      <LocaleProvider>
        <Workbench />
      </LocaleProvider>,
    );
    await flush(120);
    const items = [...host.querySelectorAll(".pg-sync-changes li")];
    for (const [kind, , chinese] of KINDS) {
      const item = items.find((li) => li.textContent?.includes(`ds-${kind}`))!;
      expect(item.querySelector("strong")!.textContent).toBe(chinese);
    }
    expect(host.textContent).not.toContain("sync.");
  });
});
