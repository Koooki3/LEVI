import { flush, render, setupDom, waitFor } from "../ds/__tests__/dom";
import { afterEach, describe, expect, mock, test } from "bun:test";

mock.module("next/navigation", () => ({
  useRouter: () => ({ push: () => undefined }),
  usePathname: () => "/",
}));

const { ConfirmProvider } = await import("../shell/confirm");
const { default: AgentConnections, formatGrantLifetime } =
  await import("../agent-connections");

setupDom();

const t = (text: string) => text;

describe("formatGrantLifetime", () => {
  test("a short grant is not rounded up to an hour", () => {
    expect(formatGrantLifetime(300, t)).toBe("5 min");
    expect(formatGrantLifetime(20, t)).toBe("1 min");
    expect(formatGrantLifetime(0, t)).toBe("< 1 min");
    expect(formatGrantLifetime(3600, t)).toBe("1h");
    expect(formatGrantLifetime(5400, t)).toBe("1h 30 min");
    expect(formatGrantLifetime(7 * 3600, t)).toBe("7h");
  });
});

const EXTERNAL = {
  configured: true,
  enabled: true,
  datasets: ["local/demo"],
  live: true,
  grants: [
    {
      id: "g1",
      label: "agent",
      datasets: ["local/demo"],
      calls: 3,
      expires_in_seconds: 300,
      idle_seconds: 4,
      live: true,
    },
  ],
};

const original = globalThis.fetch;
const originalInterval = globalThis.setInterval;
afterEach(() => {
  globalThis.fetch = original;
  globalThis.setInterval = originalInterval;
});
/** The card polls every 5 s; the test lets it poll every 40 ms. */
function pollFast() {
  globalThis.setInterval = ((fn: () => void, ms?: number) =>
    originalInterval(fn, ms === 5000 ? 40 : ms)) as typeof setInterval;
}

function serve(connections: () => Response) {
  globalThis.fetch = mock((url: string) =>
    Promise.resolve(
      String(url).endsWith("/connections")
        ? connections()
        : new Response(JSON.stringify({})),
    ),
  ) as unknown as typeof fetch;
}

const view = (
  <ConfirmProvider>
    <AgentConnections
      providers={[]}
      selected=""
      select={() => undefined}
      refresh={async () => undefined}
      edit={() => undefined}
    />
  </ConfirmProvider>
);

describe("the external Agent card", () => {
  test("a scoped connection with five minutes left says so", async () => {
    serve(() => new Response(JSON.stringify({ external: EXTERNAL })));
    const { host } = await render(view);
    await waitFor(() => host.textContent?.includes("expires in"));
    expect(host.textContent).toContain("expires in 5 min");
    expect(host.textContent).not.toContain("expires in 1h");
  });

  test("the 5 s status poll failing turns Connected into Status unknown", async () => {
    pollFast();
    let healthy = true;
    serve(() =>
      healthy
        ? new Response(JSON.stringify({ external: EXTERNAL }))
        : new Response("down", { status: 502 }),
    );
    const { host } = await render(view);
    await waitFor(() => host.textContent?.includes("Connected"));
    expect(host.textContent).not.toContain("Status unknown");
    healthy = false;
    await waitFor(() => host.textContent?.includes("Status unknown"), {
      timeoutMs: 8000,
      label: "the poll to fail",
    });
    expect(host.textContent).not.toContain("Connected");
    healthy = true;
    await waitFor(() => host.textContent?.includes("Connected"), {
      timeoutMs: 8000,
      label: "the poll to recover",
    });
    await flush(0);
  });
});
