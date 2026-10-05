import { flush, render, setupDom } from "@/components/ds/__tests__/dom";
import { afterEach, describe, expect, mock, test } from "bun:test";

mock.module("next/navigation", () => ({
  useRouter: () => ({ push: () => {}, replace: () => {} }),
  usePathname: () => "/",
}));

const { HomeDashboard } = await import("../home-dashboard");

setupDom();

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});

/** Every LEVI API answer comes from `answer(path)`. */
function serve(answer: (path: string) => Response) {
  globalThis.fetch = mock((input: RequestInfo | URL) =>
    Promise.resolve(answer(String(input))),
  ) as unknown as typeof fetch;
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

describe("home page data", () => {
  test("a failed read shows the error and Try again, not an empty state", async () => {
    serve(() => json({ detail: "boom" }, 500));
    const { host } = await render(<HomeDashboard />);
    await flush(50);
    const text = host.textContent ?? "";
    expect(text).toContain("The job lists could not be read.");
    expect(text).toContain("The list of local datasets could not be read.");
    expect(
      [...host.querySelectorAll("button")].filter(
        (button) => button.textContent?.trim() === "Try again",
      ),
    ).toHaveLength(3);
    expect(text).not.toContain("No job is running.");
    expect(text).not.toContain("No local dataset yet.");
  });

  test("every card's failure is the same three-part box, not a grey line", async () => {
    serve(() => json({ detail: "boom" }, 500));
    const { host } = await render(<HomeDashboard />);
    await flush(50);
    const boxes = [...host.querySelectorAll(".levi-home-card .pg-problem")];
    expect(boxes).toHaveLength(3);
    for (const box of boxes) {
      expect(box.querySelector(".pg-problem__title")).not.toBeNull();
      expect(box.querySelector(".pg-problem__why")).not.toBeNull();
      expect(box.querySelector(".pg-problem__fix button")!.textContent).toBe(
        "Try again",
      );
    }
    expect(host.querySelector(".levi-home-error")).toBeNull();
    expect(host.querySelector(".levi-home-note")).toBeNull();
  });

  test("a running job is one row that leads to its page", async () => {
    serve((path) =>
      path.includes("/catalog")
        ? json({ local: [] })
        : path.includes("/pool/jobs")
          ? json({ jobs: [] })
          : path.includes("/activity/tasks")
            ? json({ tasks: [] })
            : json([
                {
                  id: "20261005-0731-1",
                  status: "running",
                  stage: "pipeline",
                  dataset: "raw_alt_lerobot",
                  progress: { done: 3, total: 8, stage: "Convert" },
                },
              ]),
    );
    const { host } = await render(<HomeDashboard />);
    await flush(50);
    const rows = host.querySelectorAll("a.pg-home-job");
    expect(rows).toHaveLength(1);
    expect(rows[0].getAttribute("href")).toBe("/workbench");
    // The subject is written once as text: the bar's label is only for
    // assistive technology (CSS hides it), and the row ends in an arrow.
    expect(rows[0].querySelector(".pg-home-job__go")).not.toBeNull();
    expect(
      rows[0].querySelector("[role=progressbar]")!.getAttribute("aria-label"),
    ).toContain("raw_alt_lerobot");
    expect(rows[0].getAttribute("aria-label")).toContain("Conversion & review");
  });

  test("empty answers show the empty states and no error", async () => {
    serve((path) =>
      path.includes("/catalog")
        ? json({ local: [] })
        : path.includes("/pool/jobs")
          ? json({ jobs: [] })
          : path.includes("/activity/tasks")
            ? json({ tasks: [] })
            : json([]),
    );
    const { host } = await render(<HomeDashboard />);
    await flush(50);
    const text = host.textContent ?? "";
    expect(text).toContain("No job is running.");
    expect(text).toContain("No local dataset yet.");
    expect(text).not.toContain("could not be read");
  });
});
