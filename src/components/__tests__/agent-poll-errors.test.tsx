import { click, flush, render, setupDom, waitFor } from "../ds/__tests__/dom";
import { afterEach, describe, expect, mock, test } from "bun:test";
import AgentPilot from "../agent-pilot";
import AgentRuntimeConnections from "../agent-runtime-connections";

setupDom();

const originalFetch = globalThis.fetch;
const originalSource = (globalThis as { EventSource?: unknown }).EventSource;
afterEach(() => {
  globalThis.fetch = originalFetch;
  (globalThis as { EventSource?: unknown }).EventSource = originalSource;
});

const json = (body: unknown, status = 200) =>
  Promise.resolve(new Response(JSON.stringify(body), { status }));

describe("a failed status read is named as such and clears itself", () => {
  test("Pilot: its own title, the server's words, and gone after a good poll", async () => {
    (globalThis as { EventSource?: unknown }).EventSource = class {
      close() {}
    };
    let healthy = false;
    globalThis.fetch = mock((url: string) => {
      const path = String(url);
      if (path.endsWith("/pilot/sessions"))
        return healthy
          ? json([])
          : json({ detail: "pilot service unreachable" }, 502);
      if (path.endsWith("/tools")) return json({ events: [] });
      if (path.endsWith("/manifest"))
        return json({ status: "empty", artifacts: [] });
      return json([]);
    }) as unknown as typeof fetch;
    const { host } = await render(
      <AgentPilot
        runId="r1"
        approved
        repo="local/x"
        edited={false}
        follow={false}
      />,
    );
    await waitFor(
      () =>
        host.textContent?.includes("The Pilot status could not be refreshed"),
      { label: "the pilot banner" },
    );
    expect(host.textContent).toContain("pilot service unreachable");
    expect(host.textContent).not.toContain("Error: ");
    expect(host.textContent).not.toContain("The Pilot request failed");
    healthy = true;
    await waitFor(
      () =>
        !host.textContent?.includes("The Pilot status could not be refreshed"),
      { label: "the banner to clear", timeoutMs: 6000 },
    );
  });

  test("Codex / Claude connections: a later successful read clears the banner", async () => {
    let healthy = false;
    globalThis.fetch = mock(() =>
      healthy ? json([]) : json({ detail: "x" }, 500),
    ) as unknown as typeof fetch;
    const { host } = await render(<AgentRuntimeConnections />);
    await flush(20);
    expect(host.textContent).toContain(
      "The connection status could not be read",
    );
    expect(host.textContent).not.toContain("Error: ");
    healthy = true;
    const refresh = [...host.querySelectorAll("button")].find((b) =>
      b.textContent?.includes("Refresh status"),
    )!;
    await click(refresh);
    await flush(20);
    expect(host.textContent).not.toContain(
      "The connection status could not be read",
    );
  });
});
