import { act } from "react";
import { render, setupDom, waitFor } from "@/components/ds/__tests__/dom";
import { afterEach, describe, expect, test } from "bun:test";
import type { ApiResult } from "../deploy-api";
import { pollDelay, usePolled, type Polled } from "../use-polled";

setupDom();

afterEach(() => {
  delete (document as unknown as Record<string, unknown>).visibilityState;
});
function visibility(state: "hidden" | "visible") {
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    get: () => state,
  });
  document.dispatchEvent(new Event("visibilitychange"));
}

const ok = (data: unknown): ApiResult => ({ ok: true, status: 200, data });
const down: ApiResult = {
  ok: false,
  status: 0,
  code: "network",
  message: "down",
  extra: {},
  field: null,
  fields: [],
};

function harness(
  read: () => Promise<ApiResult>,
  { pollMs = 15, freshMs = 1000 } = {},
) {
  const seen: { latest: Polled<number> | null } = { latest: null };
  function Probe() {
    const polled = usePolled<number>({
      read: () => read(),
      parse: (data) => (typeof data === "number" ? data : null),
      pollMs,
      freshMs,
    });
    seen.latest = polled;
    return <p data-testid="v">{String(polled.value)}</p>;
  }
  return { seen, element: <Probe /> };
}

describe("one polled document", () => {
  test("reads again and again while the tab is visible, and the answer is current", async () => {
    let n = 0;
    const { seen, element } = harness(async () => ok(++n));
    await render(element);
    await waitFor(() => (seen.latest?.value ?? 0) >= 3, {
      label: "three reads",
    });
    expect(seen.latest!.current).toBe(true);
    expect(seen.latest!.lastOk).not.toBeNull();
  });

  test("nothing is asked while the tab is hidden, and one read is made at once when it returns", async () => {
    let n = 0;
    const { seen, element } = harness(async () => ok(++n));
    await render(element);
    await waitFor(() => n >= 2, { label: "reads" });
    await act(async () => visibility("hidden"));
    const at = n;
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 120));
    });
    // At most the read that was in flight when the tab was hidden.
    expect(n - at).toBeLessThanOrEqual(1);
    const after = n;
    await act(async () => visibility("visible"));
    await waitFor(() => n > after, { label: "the read on return" });
    expect(seen.latest!.current).toBe(true);
  });

  test("an answer from before the tab was hidden is not shown as current when it returns", async () => {
    let n = 0;
    let hold: (() => void) | null = null;
    const { seen, element } = harness(async () => {
      n += 1;
      if (n >= 3)
        await new Promise<void>((resolve) => {
          hold = resolve;
        });
      return ok(n);
    });
    await render(element);
    await waitFor(() => n >= 3, { label: "the third read starts" });
    await act(async () => visibility("hidden"));
    await act(async () => visibility("visible"));
    // The read in flight on return has not answered: the old value is not current.
    expect(seen.latest!.current).toBe(false);
    await act(async () => {
      hold?.();
    });
  });

  test("a failed read says why, keeps the last answer for a while and drops it when it is too old", async () => {
    let fail = false;
    const { seen, element } = harness(async () => (fail ? down : ok(7)), {
      pollMs: 10,
      freshMs: 120,
    });
    await render(element);
    await waitFor(() => seen.latest?.value === 7, { label: "first answer" });
    fail = true;
    await waitFor(() => seen.latest?.problem !== null, {
      label: "the failure",
    });
    expect(seen.latest!.current).toBe(false);
    expect(seen.latest!.problem).toMatchObject({ code: "network" });
    await waitFor(() => seen.latest?.value === null, {
      label: "the old answer to be dropped",
    });
  });

  test("an answer in another format is a problem, not a value", async () => {
    const { seen, element } = harness(async () => ok("not a number"));
    await render(element);
    await waitFor(() => seen.latest?.loaded === true, { label: "loaded" });
    expect(seen.latest!.value).toBeNull();
    expect(seen.latest!.problem).toEqual({ format: true });
  });

  test("refresh reads now and resolves when that read has finished", async () => {
    let n = 0;
    const { seen, element } = harness(async () => ok(++n), { pollMs: 5000 });
    await render(element);
    await waitFor(() => seen.latest?.value === 1, { label: "first read" });
    await act(async () => {
      await seen.latest!.refresh();
    });
    expect(seen.latest!.value).toBe(2);
  });

  test("the wait doubles after each failure, up to 30 s", () => {
    expect(pollDelay(0, 2000)).toBe(2000);
    expect(pollDelay(1, 2000)).toBe(4000);
    expect(pollDelay(2, 2000)).toBe(8000);
    expect(pollDelay(9, 2000)).toBe(30000);
    expect(pollDelay(3, 1000)).toBe(8000);
  });
});
