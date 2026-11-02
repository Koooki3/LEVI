import { render, setupDom } from "@/components/ds/__tests__/dom";
import { act } from "react";
import { afterEach, beforeEach, describe, expect, jest, test } from "bun:test";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import { LocaleProvider } from "@/components/levi-locale";
import { LIVE_KEEP_MS, LIVE_POLL_MS, LiveFrames } from "../live-frames";
import type { FrameCamera, FrameReason, FrameResult } from "../run-api";
import { RUN_STATES, hasPictures } from "../run-logic";

// The live view: both cameras of the episode being recorded, side on the left
// and wrist on the right, asked for only while the run is active and the tab
// is visible, one request at a time per camera, every picture an object URL
// that is revoked. The api is a fake (`frame`); nothing is fetched.

afterEach(() => jest.useRealTimers());
setupDom();

let made: string[] = [];
let revoked: string[] = [];
const realCreate = URL.createObjectURL;
const realRevoke = URL.revokeObjectURL;

beforeEach(() => {
  made = [];
  revoked = [];
  URL.createObjectURL = ((blob: Blob) => {
    const url = `blob:test/${made.length}-${blob.size}`;
    made.push(url);
    return url;
  }) as typeof URL.createObjectURL;
  URL.revokeObjectURL = ((url: string) => {
    revoked.push(url);
  }) as typeof URL.revokeObjectURL;
});
// Registered after setupDom's own: the card is unmounted (and revokes) first.
afterEach(() => {
  URL.createObjectURL = realCreate;
  URL.revokeObjectURL = realRevoke;
  Reflect.deleteProperty(document, "visibilityState");
  try {
    localStorage.clear();
  } catch {
    // no storage in this DOM
  }
});

const picture = (
  text: string,
  over: Partial<Extract<FrameResult, { ok: true }>> = {},
): FrameResult => ({
  ok: true,
  blob: new Blob([text], { type: "image/jpeg" }),
  ageMs: 200,
  episode: "demo_0003",
  ...over,
});
const missing = (reason: FrameReason): FrameResult => ({ ok: false, reason });

type Script = (
  camera: FrameCamera,
  n: number,
  signal?: AbortSignal,
) => FrameResult | Promise<FrameResult>;

function fakeApi(script: Script) {
  const calls: FrameCamera[] = [];
  const signals: Array<AbortSignal | undefined> = [];
  const api = {
    frame: (camera: FrameCamera, signal?: AbortSignal) => {
      calls.push(camera);
      signals.push(signal);
      return Promise.resolve(
        script(camera, calls.filter((c) => c === camera).length, signal),
      );
    },
  };
  return {
    api,
    calls,
    signals,
    count: (camera: FrameCamera) => calls.filter((c) => c === camera).length,
  };
}

const flush = () =>
  act(async () => {
    for (let i = 0; i < 20; i += 1) await Promise.resolve();
  });
/** `ticks` poll periods, one at a time: an answer lands between two ticks. */
async function wait(ticks: number, ms = LIVE_POLL_MS) {
  for (let i = 0; i < ticks; i += 1)
    await act(async () => {
      jest.advanceTimersByTime(ms);
      for (let j = 0; j < 20; j += 1) await Promise.resolve();
    });
}

function visibility(state: "hidden" | "visible") {
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    get: () => state,
  });
  return act(async () => {
    document.dispatchEvent(new Event("visibilitychange"));
    for (let i = 0; i < 20; i += 1) await Promise.resolve();
  });
}

async function open(
  script: Script,
  {
    active = true,
    zhLanguage = false,
    keepMs,
  }: { active?: boolean; zhLanguage?: boolean; keepMs?: number } = {},
) {
  jest.useFakeTimers();
  if (zhLanguage) localStorage.setItem("levi-language", "zh");
  const fake = fakeApi(script);
  const element = (on: boolean) => {
    const card = <LiveFrames active={on} api={fake.api} keepMs={keepMs} />;
    return zhLanguage ? <LocaleProvider>{card}</LocaleProvider> : card;
  };
  const view = await render(element(active));
  await flush();
  return {
    ...view,
    ...fake,
    setActive: async (on: boolean) => {
      await view.rerender(element(on));
      await flush();
    },
    unmount: async () => {
      await view.rerender(<div />);
      await flush();
    },
  };
}
type H = Awaited<ReturnType<typeof open>>;

const box = (h: H, camera: FrameCamera) =>
  h.host.querySelector<HTMLElement>(`[data-testid=marvel0-live-${camera}]`)!;
const img = (h: H, camera: FrameCamera) =>
  box(h, camera).querySelector<HTMLImageElement>("img");
const empty = (h: H, camera: FrameCamera) =>
  h.host.querySelector<HTMLElement>(
    `[data-testid=marvel0-live-${camera}-empty]`,
  );
const meta = (h: H, camera: FrameCamera) =>
  h.host.querySelector<HTMLElement>(
    `[data-testid=marvel0-live-${camera}-meta]`,
  )!;

describe("the live view", () => {
  test("asks about ten times a second, and only while the run is active", async () => {
    const h = await open(() => picture("x"), { active: false });
    await wait(8);
    expect(h.calls).toEqual([]);
    await h.setActive(true);
    expect(h.count("side")).toBe(1); // at once
    expect(h.count("wrist")).toBe(1);
    await wait(4); // 0.4 s: one ask per camera per 100 ms
    expect(h.count("side")).toBe(5);
    expect(h.count("wrist")).toBe(5);
    await h.setActive(false);
    await wait(8);
    expect(h.count("side")).toBe(5);
    expect(h.count("wrist")).toBe(5);
    expect(LIVE_POLL_MS).toBe(100);
  });

  test("the third-person view is on the left and the wrist view on the right", async () => {
    const h = await open((camera) => picture(camera === "side" ? "S" : "WW"));
    const side = box(h, "side");
    const wrist = box(h, "wrist");
    expect(
      side.compareDocumentPosition(wrist) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(side.parentElement).toBe(wrist.parentElement);
    expect(side.parentElement?.className).toContain("rg-live__grid");
    expect(side.querySelector("figcaption")?.textContent).toBe(
      en["marvel0.live.side"],
    );
    expect(wrist.querySelector("figcaption")?.textContent).toBe(
      en["marvel0.live.wrist"],
    );
    // each box shows its own camera's picture, with a text alternative
    expect(img(h, "side")?.getAttribute("src")).toBe(made[0]);
    expect(made[0]).toEndWith("-1"); // the one byte of "S"
    expect(img(h, "wrist")?.getAttribute("src")).toBe(made[1]);
    expect(made[1]).toEndWith("-2");
    expect(img(h, "side")?.alt).toBe(en["marvel0.live.alt.side"]);
    expect(img(h, "wrist")?.alt).toBe(en["marvel0.live.alt.wrist"]);
  });

  test("says how old the picture is and which episode it is of", async () => {
    const h = await open((camera) =>
      camera === "side"
        ? picture("a", { ageMs: 200 })
        : picture("b", { ageMs: 12_400, episode: "demo_0012" }),
    );
    expect(meta(h, "side").textContent).toBe("0.2 s ago · demo_0003");
    expect(meta(h, "side").dataset.late).toBe("false");
    expect(meta(h, "wrist").textContent).toBe("12 s ago · demo_0012");
    expect(meta(h, "wrist").dataset.late).toBe("true");
  });

  test("a tab that is hidden is not asked for anything; showing it asks at once", async () => {
    const h = await open(() => picture("x"));
    expect(h.count("side")).toBe(1);
    await visibility("hidden");
    await wait(8);
    expect(h.count("side")).toBe(1);
    expect(h.count("wrist")).toBe(1);
    await visibility("visible");
    expect(h.count("side")).toBe(2);
    expect(h.count("wrist")).toBe(2);
    await wait(2);
    expect(h.count("side")).toBe(4);
  });

  test("a tab hidden mid-request gives that request up", async () => {
    let held: AbortSignal | undefined;
    const h = await open((camera, n, signal) => {
      if (camera === "side" && n === 1) {
        held = signal;
        return new Promise<FrameResult>(() => {});
      }
      return picture("x");
    });
    expect(held?.aborted).toBe(false);
    await visibility("hidden");
    expect(held?.aborted).toBe(true);
    await visibility("visible");
    expect(h.count("side")).toBe(2); // not blocked by the abandoned one
  });

  test("never a second request of a camera while one is in flight", async () => {
    const release: Array<(result: FrameResult) => void> = [];
    const h = await open((camera) =>
      camera === "side"
        ? new Promise<FrameResult>((resolve) => release.push(resolve))
        : picture("w"),
    );
    await wait(12); // three seconds of ticks
    expect(h.count("side")).toBe(1);
    expect(h.count("wrist")).toBe(13); // the other camera is not held by it
    await act(async () => {
      release[0](picture("s"));
      for (let i = 0; i < 20; i += 1) await Promise.resolve();
    });
    await wait(1);
    expect(h.count("side")).toBe(2);
  });

  test("a camera without a picture says why, in the reader's language", async () => {
    const reasons: FrameReason[] = [
      "no_run",
      "no_episode",
      "no_frame",
      "unavailable",
    ];
    for (const reason of reasons) {
      const h = await open(() => missing(reason));
      const key = `marvel0.live.${reason}` as keyof typeof en;
      expect(empty(h, "side")?.textContent).toBe(en[key]);
      expect(empty(h, "wrist")?.textContent).toBe(en[key]);
      expect(empty(h, "side")?.dataset.reason).toBe(reason);
      expect(img(h, "side")).toBeNull();
      expect(made).toEqual([]);
    }
    const h = await open(() => missing("no_episode"), { zhLanguage: true });
    expect(empty(h, "side")?.textContent).toBe(zh["marvel0.live.no_episode"]);
    expect(h.host.textContent).toContain(zh["marvel0.live.side"]);
    expect(h.host.textContent).toContain(zh["marvel0.live.wrist"]);
  });

  test("before any answer it waits, and with no run it says pictures come with a run", async () => {
    const h = await open(() => new Promise<FrameResult>(() => {}));
    expect(empty(h, "side")?.textContent).toBe(en["marvel0.live.wait"]);
    const quiet = await open(() => picture("x"), { active: false });
    expect(empty(quiet, "side")?.textContent).toBe(en["marvel0.live.no_run"]);
    expect(empty(quiet, "wrist")?.dataset.reason).toBe("no_run");
  });

  test("a missing picture leaves the last one a moment, then gives it up", async () => {
    const h = await open(
      (camera, n) => (n <= 2 ? picture("p") : missing("no_episode")),
      {
        keepMs: 1_000,
      },
    );
    await wait(1);
    expect(h.count("side")).toBe(2);
    await wait(1); // the first miss
    expect(h.count("side")).toBe(3);
    expect(img(h, "side")).not.toBeNull();
    expect(empty(h, "side")).toBeNull();
    expect(meta(h, "side").textContent).toBe(""); // no age for what is no longer current
    await wait(6);
    expect(img(h, "side")).not.toBeNull(); // 700 ms after the last picture (keep 1 s)
    await wait(5);
    expect(img(h, "side")).toBeNull(); // 1.2 s after it
    expect(empty(h, "side")?.textContent).toBe(en["marvel0.live.no_episode"]);
    // the one picture kept and the earlier one were both let go of
    expect(revoked.sort()).toEqual([...made].sort().slice(0, revoked.length));
    expect(revoked).toContain(made[made.length - 1]);
    expect(LIVE_KEEP_MS).toBe(3_000);
  });

  test("a picture is back at once when the next one comes", async () => {
    const h = await open((camera, n) =>
      n === 2 ? missing("no_frame") : picture(`q${n}`),
    );
    await wait(1);
    expect(empty(h, "wrist")).toBeNull();
    await wait(1);
    expect(img(h, "wrist")).not.toBeNull();
    expect(meta(h, "wrist").textContent).toContain("demo_0003");
  });

  test("every object URL is revoked once: replaced, run over, card gone", async () => {
    const h = await open((camera, n) => picture(`${camera}${n}`));
    await wait(3);
    // four pictures per camera so far; all but the two on screen are revoked
    expect(made).toHaveLength(8);
    expect(revoked).toHaveLength(6);
    expect(revoked).not.toContain(img(h, "side")?.getAttribute("src"));
    expect(revoked).not.toContain(img(h, "wrist")?.getAttribute("src"));
    await h.setActive(false);
    expect(revoked).toHaveLength(8);
    expect(img(h, "side")).toBeNull();
    await h.setActive(true);
    await wait(1);
    await h.unmount();
    expect(new Set(revoked).size).toBe(made.length);
    expect(revoked).toHaveLength(made.length); // none twice
  });

  test("stops on unmount: no request, nothing left to revoke", async () => {
    const h = await open(() => picture("x"));
    await wait(2);
    const before = h.calls.length;
    const signals = h.signals.slice();
    await h.unmount();
    await wait(8);
    expect(h.calls).toHaveLength(before);
    expect(new Set(revoked).size).toBe(made.length);
    expect(signals.length).toBe(before);
  });

  test("the pictures of a run that ended are not shown again when the next run starts", async () => {
    const h = await open((camera, n) =>
      n === 1 ? picture("old") : new Promise<FrameResult>(() => {}),
    );
    expect(img(h, "side")).not.toBeNull();
    await h.setActive(false);
    await h.setActive(true);
    expect(img(h, "side")).toBeNull();
    expect(img(h, "wrist")).toBeNull();
    expect(empty(h, "side")?.textContent).toBe(en["marvel0.live.wait"]);
  });

  test("an answer that lands after the run ended is dropped, not shown", async () => {
    const release: Array<(result: FrameResult) => void> = [];
    const h = await open(
      () => new Promise<FrameResult>((resolve) => release.push(resolve)),
    );
    await h.setActive(false);
    await act(async () => {
      release.forEach((resolve) => resolve(picture("late")));
      for (let i = 0; i < 20; i += 1) await Promise.resolve();
    });
    expect(made).toEqual([]);
    expect(img(h, "side")).toBeNull();
    expect(empty(h, "side")?.dataset.reason).toBe("no_run");
  });
});

describe("when the page asks for pictures", () => {
  test("in every state with a client, in none without one", () => {
    const withClient = RUN_STATES.filter(hasPictures);
    expect(withClient).toEqual([
      "starting",
      "at_gate",
      "homing",
      "running",
      "awaiting_label",
      "finishing",
      "held",
      "lost",
    ]);
    for (const state of ["idle", "preflight", "done", "failed"] as const)
      expect(hasPictures(state)).toBe(false);
  });
});
