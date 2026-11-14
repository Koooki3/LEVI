import {
  click,
  flush,
  render,
  setupDom,
  waitFor,
} from "@/components/ds/__tests__/dom";
import { afterEach, describe, expect, mock, test } from "bun:test";

mock.module("next/navigation", () => ({
  useRouter: () => ({ push: () => {}, replace: () => {} }),
  usePathname: () => "/workbench",
}));
mock.module("next/link", () => ({
  default: ({ children, ...props }: Record<string, unknown>) => (
    <a {...(props as object)}>{children as never}</a>
  ),
}));

const { default: Workbench } = await import("../page");
const { ConfirmProvider } = await import("@/components/shell/confirm");

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

const OWN = {
  id: "local/own",
  name: "own",
  path: "/w/datasets/own",
  kind: "lerobot",
};

function serve(removal: Response) {
  globalThis.fetch = mock((input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    if (init?.method === "DELETE") return Promise.resolve(removal);
    if (path.includes("/convert/formats"))
      return Promise.resolve(
        json({ inputs: [], outputs: [], unsupported: [] }),
      );
    if (path.includes("/catalog"))
      return Promise.resolve(
        json({
          local: [OWN],
          workspace: "/w",
          conversion_available: true,
          stages: [],
          linked: [],
        }),
      );
    if (path.includes("/sync"))
      return Promise.resolve(
        json({
          enabled: true,
          running: true,
          interval_seconds: 5,
          last_scan: null,
          last_error: null,
          pending: [],
          changes: [],
        }),
      );
    return Promise.resolve(json([]));
  }) as unknown as typeof fetch;
}

const button = (root: ParentNode, label: string) =>
  [...root.querySelectorAll("button")].find((b) =>
    b.textContent?.includes(label),
  ) as HTMLButtonElement | undefined;

async function unregister(host: HTMLElement) {
  const row = await waitFor(() => host.querySelector("tbody tr"));
  await click(button(row, "Unregister")!);
  const dialog = document.querySelector('[role="alertdialog"]')!;
  await click(button(dialog, "Unregister")!);
  await flush(30);
}

describe("Unregister reports a refused removal", () => {
  test("a 409 from the server shows the problem, not silence", async () => {
    serve(json({ detail: "The dataset is open in a running job." }, 409));
    const { host } = await render(
      <ConfirmProvider>
        <Workbench />
      </ConfirmProvider>,
    );
    await unregister(host);
    expect(host.textContent).toContain(
      "The dataset was not removed from the list",
    );
    expect(host.textContent).toContain("The dataset is open in a running job.");
  });

  test("a 200 shows no problem", async () => {
    serve(json({ removed: true }));
    const { host } = await render(
      <ConfirmProvider>
        <Workbench />
      </ConfirmProvider>,
    );
    await unregister(host);
    expect(host.textContent).not.toContain(
      "The dataset was not removed from the list",
    );
  });
});
