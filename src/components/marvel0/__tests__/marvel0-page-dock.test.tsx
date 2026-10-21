import { render, setupDom, waitFor } from "@/components/ds/__tests__/dom";
import { afterEach, describe, expect, test } from "bun:test";
import { RUN, runDoc } from "./ctl-fixtures";

// The page with its own hooks (no stand-ins, unlike marvel0-page.test.tsx):
// the HOLD dock must not wait for the status poll. A reload while the hosted
// client runs has to show HOLD at once, even when /setup/status and
// /marvel0/status have not answered (or never do).
const { default: Marvel0Page } = await import("@/app/marvel_workstation/page");
setupDom();

const SETUP = "/api/levi/setup/status";
const MARVEL0 = "/api/levi/marvel0/status";
const DOCK_HOLD = "[data-testid=marvel0-run-dock-hold]";

let restore = () => {};
afterEach(() => {
  restore();
  restore = () => {};
  try {
    localStorage.clear();
  } catch {
    // no storage in this DOM
  }
});

/** The two status routes never settle; the run read answers `run`; any other
 * route (the arm stack) is a 404. */
function serve(run: unknown) {
  const original = globalThis.fetch;
  const paths: string[] = [];
  globalThis.fetch = (async (input: unknown) => {
    const path = String(input);
    paths.push(path);
    if (path === SETUP || path === MARVEL0)
      return new Promise<Response>(() => {});
    if (path === RUN) return new Response(JSON.stringify(run), { status: 200 });
    return new Response("{}", { status: 404 });
  }) as typeof fetch;
  restore = () => {
    globalThis.fetch = original;
  };
  return paths;
}

describe("the marvel0 page before the status poll has answered", () => {
  test("a running client shows the HOLD dock at once while the page still says Loading…", async () => {
    const paths = serve(runDoc("running"));
    const { host } = await render(<Marvel0Page />);
    const hold = await waitFor(
      () => host.querySelector(DOCK_HOLD) as HTMLButtonElement | null,
      // Shorter than the test's own 5 s, so a miss reports what was missing.
      { label: "the HOLD dock", timeoutMs: 3000 },
    );
    expect(hold.disabled).toBe(false);
    expect(
      host
        .querySelector("[data-testid=marvel0-run-dock]")!
        .getAttribute("data-state"),
    ).toBe("running");
    // The status poll really is still waiting: both of its routes were asked,
    // none answered, and the body is on its first round.
    expect(paths).toContain(SETUP);
    expect(paths).toContain(MARVEL0);
    expect(host.querySelector("[aria-busy=true]")!.textContent).toBe(
      "Loading…",
    );
    expect(host.querySelector("[data-testid=marvel0-idle]")).toBeNull();
  });

  test("an idle client shows no dock while the page loads (nothing to hold)", async () => {
    const paths = serve(runDoc("idle_ready"));
    const { host } = await render(<Marvel0Page />);
    await waitFor(() => (paths.includes(RUN) ? true : null), {
      label: "the run read",
    });
    await new Promise((resolve) => setTimeout(resolve, 60));
    expect(host.querySelector("[aria-busy=true]")).not.toBeNull();
    expect(host.querySelector("[data-testid=marvel0-run-dock]")).toBeNull();
  });
});
