import { render, setupDom, waitFor, flush } from "../../ds/__tests__/dom";
import { act } from "react";
import { afterEach, describe, expect, mock, test } from "bun:test";
import { useState } from "react";
import { Freshness } from "../freshness";
import { useJobPolling } from "../job-panel";
import type { PoolJob } from "../types";

setupDom();

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
  Object.defineProperty(document, "visibilityState", {
    value: "visible",
    configurable: true,
  });
});

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

function Harness() {
  const [job, setJob] = useState<PoolJob>({
    id: "export-1",
    kind: "export",
    status: "running",
  });
  const poll = useJobPolling(job, setJob);
  return (
    <div>
      <output id="status">{job.status}</output>
      <Freshness
        onlyWhenFailing
        lastOk={poll.lastOk}
        delay={poll.delay}
        failures={poll.failures}
      />
    </div>
  );
}

describe("following one job", () => {
  test("a failed request is said, retried at the same pace, and cleared by a success", async () => {
    let n = 0;
    globalThis.fetch = mock(() => {
      n += 1;
      return Promise.resolve(
        n === 1
          ? json({ detail: "down" }, 502)
          : json({ id: "export-1", kind: "export", status: "done" }),
      );
    }) as unknown as typeof fetch;
    const { host } = await render(<Harness />);
    expect(host.querySelector("p")).toBeNull();
    await waitFor(() => host.querySelector("p")?.textContent, {
      label: "the failed-request note",
    });
    expect(host.querySelector("p")!.textContent).toContain(
      "last request failed",
    );
    // The poll was not lost with the failure: the next one reaches the job.
    await waitFor(() => host.querySelector("#status")?.textContent === "done", {
      label: "the retried request",
    });
    expect(host.querySelector("p")).toBeNull();
  }, 15000);

  test("asks nothing while the tab is hidden", async () => {
    let n = 0;
    globalThis.fetch = mock(() => {
      n += 1;
      return Promise.resolve(
        json({ id: "export-1", kind: "export", status: "done" }),
      );
    }) as unknown as typeof fetch;
    Object.defineProperty(document, "visibilityState", {
      value: "hidden",
      configurable: true,
    });
    await render(<Harness />);
    await flush(1600);
    expect(n).toBe(0);
    Object.defineProperty(document, "visibilityState", {
      value: "visible",
      configurable: true,
    });
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await waitFor(() => n === 1, { label: "a request on return" });
  }, 15000);
});
