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
    ).toHaveLength(2);
    expect(text).not.toContain("No job is running.");
    expect(text).not.toContain("No local dataset yet.");
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
