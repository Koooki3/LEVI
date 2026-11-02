import {
  click,
  render,
  setupDom,
  waitFor,
} from "@/components/ds/__tests__/dom";
import { act } from "react";
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import { LocaleProvider } from "@/components/levi-locale";
import { LiveFrames } from "../live-frames";
import { useCameraPreview } from "../use-camera-preview";
import type { PreviewApi } from "../preview-api";
import type { ApiResult } from "../deploy-api";
import type { FrameCamera, FrameResult } from "../run-api";
import { nothing, ok, refused } from "./actions-helpers";

// The live view with the camera preview: with no run, a button asks LEVI to
// open the cameras; while the preview holds them both pictures are shown (from
// `preview/frame`, not the run's route) and the same button stops it; the
// reasons it cannot start are shown; when a run is active the preview's
// controls and reads are gone. The apis are fakes; nothing is fetched.

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
afterEach(() => {
  URL.createObjectURL = realCreate;
  URL.revokeObjectURL = realRevoke;
  try {
    localStorage.clear();
  } catch {
    // no storage in this DOM
  }
});

const doc = (state: string, over: Record<string, unknown> = {}) => ({
  schema: "levi.marvel0.preview.v1",
  state,
  reason: null,
  may_start: state === "off",
  blocked_by: null,
  stopped_by: null,
  frames: { side: { age_ms: null }, wrist: { age_ms: null } },
  limits: { idle_s: 20, max_s: 600 },
  remaining: null,
  ...over,
});
const picture = (text: string): FrameResult => ({
  ok: true,
  blob: new Blob([text], { type: "image/jpeg" }),
  ageMs: 150,
  episode: null,
});

function fakePreview(initial: Record<string, unknown> = doc("off")) {
  const s = {
    doc: initial as Record<string, unknown>,
    readAnswer: null as ApiResult | null,
    startAnswer: null as ApiResult | null,
    /** Reads wait for this when it is set (a read that hangs). */
    hold: null as Promise<void> | null,
    /** Starts wait for this when it is set (a request still in flight). */
    startGate: null as Promise<void> | null,
    reads: 0,
    starts: 0,
    stops: 0,
    frames: [] as FrameCamera[],
    onFrame: ((camera) => picture(camera === "side" ? "S" : "WW")) as (
      camera: FrameCamera,
    ) => FrameResult,
  };
  const api: PreviewApi = {
    read: async () => {
      s.reads += 1;
      if (s.hold) await s.hold;
      return s.readAnswer ?? ok(s.doc);
    },
    start: async () => {
      s.starts += 1;
      if (s.startGate) await s.startGate;
      if (s.startAnswer) return s.startAnswer;
      s.doc = doc("on", { remaining: { idle_s: 20, lifetime_s: 600 } });
      return ok(s.doc, 202);
    },
    stop: async () => {
      s.stops += 1;
      s.doc = doc("off", { stopped_by: "requested" });
      return ok(s.doc);
    },
    frame: async (camera) => {
      s.frames.push(camera);
      return s.onFrame(camera);
    },
  };
  return { api, s };
}

function fakeRun() {
  const calls: FrameCamera[] = [];
  return {
    calls,
    api: {
      frame: async (camera: FrameCamera): Promise<FrameResult> => {
        calls.push(camera);
        return picture("R");
      },
    },
  };
}

async function open(
  preview: ReturnType<typeof fakePreview> | null,
  { active = false, zhLanguage = false } = {},
) {
  if (zhLanguage) localStorage.setItem("levi-language", "zh");
  const run = fakeRun();
  const element = (on: boolean) => {
    const card = (
      <LiveFrames
        active={on}
        api={run.api}
        preview={preview?.api}
        pollMs={60_000}
      />
    );
    return zhLanguage ? <LocaleProvider>{card}</LocaleProvider> : card;
  };
  const view = await render(element(active));
  await act(async () => {
    for (let i = 0; i < 20; i += 1) await Promise.resolve();
  });
  return {
    ...view,
    run,
    setActive: async (on: boolean) => {
      await view.rerender(element(on));
      await act(async () => {
        for (let i = 0; i < 20; i += 1) await Promise.resolve();
      });
    },
  };
}
type H = Awaited<ReturnType<typeof open>>;

const q = (h: H, id: string) =>
  h.host.querySelector<HTMLElement>(`[data-testid=${id}]`);
const openButton = (h: H) =>
  q(h, "marvel0-preview-open") as HTMLButtonElement | null;
const stopButton = (h: H) =>
  q(h, "marvel0-preview-stop") as HTMLButtonElement | null;
const img = (h: H, camera: FrameCamera) =>
  q(h, `marvel0-live-${camera}`)?.querySelector("img") ?? null;
const empty = (h: H, camera: FrameCamera) =>
  q(h, `marvel0-live-${camera}-empty`);
const settle = () =>
  act(async () => {
    for (let i = 0; i < 30; i += 1) await Promise.resolve();
  });

describe("the camera preview in the live view", () => {
  test("a card handed no preview offers none and reads none", async () => {
    const h = await open(null);
    expect(q(h, "marvel0-preview")).toBeNull();
    expect(openButton(h)).toBeNull();
    expect(h.host.textContent).toContain(en["marvel0.live.no_run"]);
    expect(h.host.textContent).toContain(en["marvel0.live.lead"]);
  });

  test("with no run it reads the preview and offers to open the cameras", async () => {
    const p = fakePreview();
    const h = await open(p);
    expect(p.s.reads).toBeGreaterThan(0);
    const button = openButton(h)!;
    expect(button.tagName).toBe("BUTTON");
    expect(button.getAttribute("type")).toBe("button");
    expect(button.disabled).toBe(false);
    expect(button.textContent).toBe(en["marvel0.preview.open"]);
    expect(q(h, "marvel0-preview")?.dataset.state).toBe("off");
    expect(q(h, "marvel0-preview-status")?.textContent).toBe(
      en["marvel0.preview.status.off"],
    );
    expect(h.host.textContent).toContain(en["marvel0.preview.lead"]);
    expect(empty(h, "side")?.textContent).toBe(en["marvel0.preview.idle"]);
    expect(empty(h, "side")?.dataset.reason).toBe("idle");
    // nothing opened by looking: no start, no picture asked for
    expect(p.s.starts).toBe(0);
    expect(p.s.frames).toEqual([]);
  });

  test("Open starts it, both pictures come from the preview's route, and the same button stops it", async () => {
    const p = fakePreview();
    const h = await open(p);
    const button = openButton(h)!;
    await click(button);
    await waitFor(() => img(h, "side") && img(h, "wrist"), {
      label: "both pictures",
    });
    expect(p.s.starts).toBe(1);
    expect(h.run.calls).toEqual([]); // not the run's route
    expect(new Set(p.s.frames)).toEqual(new Set(["side", "wrist"]));
    expect(img(h, "side")?.alt).toBe(en["marvel0.live.alt.side"]);
    expect(made[0]).toEndWith("-1");
    expect(made[1]).toEndWith("-2");
    // the same element is now Stop (the keyboard focus stays on it)
    const stop = stopButton(h)!;
    expect(stop).toBe(button);
    expect(stop.textContent).toBe(en["marvel0.preview.stop"]);
    expect(q(h, "marvel0-preview")?.dataset.state).toBe("on");
    expect(q(h, "marvel0-preview-status")?.textContent).toBe(
      en["marvel0.preview.status.on"]
        .replace("{idle}", "20")
        .replace("{minutes}", "10"),
    );
    await click(stop);
    expect(p.s.stops).toBe(1);
    await waitFor(() => !img(h, "side") && openButton(h), {
      label: "the pictures gone",
    });
    expect(openButton(h)).toBe(button);
    // every picture shown was let go of
    expect(new Set(revoked).size).toBe(made.length);
    expect(empty(h, "side")?.textContent).toBe(en["marvel0.preview.idle"]);
    // a stop the person asked for needs no note
    expect(q(h, "marvel0-preview-stopped")).toBeNull();
  });

  test("while the cameras are opening the pictures are asked for and say they are not there yet", async () => {
    const p = fakePreview(doc("starting"));
    p.s.onFrame = () => ({ ok: false, reason: "no_frame" });
    const h = await open(p);
    await waitFor(() => p.s.frames.length >= 2, { label: "frames asked" });
    expect(stopButton(h)).not.toBeNull();
    expect(empty(h, "side")?.textContent).toBe(
      en["marvel0.preview.frame.no_frame"],
    );
    expect(q(h, "marvel0-preview-status")?.textContent).toBe(
      en["marvel0.preview.status.starting"],
    );
  });

  test("while it stops the button waits: busy, not natively disabled, so the focus stays", async () => {
    const p = fakePreview(doc("stopping"));
    const h = await open(p);
    const stop = stopButton(h)!;
    expect(stop.disabled).toBe(false);
    expect(stop.getAttribute("aria-disabled")).toBe("true");
    expect(stop.getAttribute("aria-busy")).toBe("true");
    expect(q(h, "marvel0-preview-status")?.textContent).toBe(
      en["marvel0.preview.status.stopping"],
    );
    await click(stop);
    expect(p.s.stops).toBe(0); // a click on it sends nothing
    expect(p.s.frames).toEqual([]); // no pictures are asked for while it lets go
  });

  test("the one button keeps the keyboard focus through on, stopping and off", async () => {
    const p = fakePreview(doc("on"));
    const h = await open(p);
    const button = stopButton(h)!;
    button.focus();
    expect(document.activeElement).toBe(button);
    p.s.doc = doc("stopping");
    await waitFor(() => q(h, "marvel0-preview")?.dataset.state === "stopping", {
      label: "the stopping state",
    });
    expect(stopButton(h)).toBe(button);
    expect(document.activeElement).toBe(button);
    p.s.doc = doc("off", { stopped_by: "requested" });
    await waitFor(() => openButton(h), { label: "Open again" });
    expect(openButton(h)).toBe(button);
    expect(document.activeElement).toBe(button);
  });

  test("a double click sends one start and the button says it is busy", async () => {
    const p = fakePreview();
    let release: () => void = () => {};
    p.s.startGate = new Promise<void>((resolve) => (release = resolve));
    const h = await open(p);
    const button = openButton(h)!;
    await click(button);
    await click(button);
    expect(p.s.starts).toBe(1);
    expect(button.getAttribute("aria-busy")).toBe("true");
    await act(async () => {
      release();
    });
    await waitFor(() => stopButton(h), { label: "Stop after the start" });
    expect(p.s.starts).toBe(1);
  });
});

describe("when the preview cannot start", () => {
  test.each([
    "run_active",
    "aeri_active",
    "client_connected",
    "arm_in_use",
    "state_unreadable",
    "client_starting",
  ])(
    "%s: the button is off and the reason is written beside it",
    async (code) => {
      const p = fakePreview(
        doc("refused", {
          reason: { code, text: "the server's sentence" },
          blocked_by: [code],
        }),
      );
      const h = await open(p);
      const button = openButton(h)!;
      expect(button.disabled).toBe(true);
      const reason = q(h, "marvel0-preview-reason")!;
      expect(reason.id).toBe(button.getAttribute("aria-describedby")!);
      expect(reason.dataset.code).toBe(code);
      expect(reason.textContent).toBe(
        en[`marvel0.preview.reason.${code}` as keyof typeof en],
      );
      await click(button);
      expect(p.s.starts).toBe(0);
    },
  );

  test("the reasons are worded in Chinese too", async () => {
    const p = fakePreview(
      doc("refused", {
        reason: { code: "arm_in_use", text: "english" },
        blocked_by: ["arm_in_use"],
      }),
    );
    const h = await open(p, { zhLanguage: true });
    expect(q(h, "marvel0-preview-reason")?.textContent).toBe(
      zh["marvel0.preview.reason.arm_in_use"],
    );
    expect(openButton(h)?.textContent).toBe(zh["marvel0.preview.open"]);
  });

  test("a reason the page has no words for shows the server's sentence, marked English", async () => {
    const p = fakePreview(
      doc("refused", { reason: { code: "brand_new", text: "A new reason." } }),
    );
    const h = await open(p);
    const reason = q(h, "marvel0-preview-reason")!;
    expect(reason.textContent).toBe("A new reason.");
    expect(reason.querySelector("[lang=en]")).not.toBeNull();
  });

  test("a failure of the cameras is shown with the reason, and Open tries again", async () => {
    const p = fakePreview(
      doc("failed", {
        reason: { code: "cameras_busy", text: "busy" },
        may_start: true,
      }),
    );
    const h = await open(p);
    expect(q(h, "marvel0-preview-reason")?.textContent).toBe(
      en["marvel0.preview.reason.cameras_busy"],
    );
    expect(q(h, "marvel0-preview")?.dataset.state).toBe("failed");
    expect(openButton(h)?.disabled).toBe(false);
    await click(openButton(h));
    expect(p.s.starts).toBe(1);
  });

  test("a failure that is also blocked does not offer a start (the server words the blocker)", async () => {
    // (the backend now sends such a document as `refused` with the blocker's
    // reason; if it ever sends `failed` with may_start false, Open stays off)
    const p = fakePreview(
      doc("failed", {
        reason: { code: "cameras_busy", text: "busy" },
        may_start: false,
        blocked_by: ["run_active"],
      }),
    );
    const h = await open(p);
    expect(openButton(h)?.disabled).toBe(true);
    await click(openButton(h));
    expect(p.s.starts).toBe(0);
  });

  test("a blocker is worded in place of an old failure, as the server sends it", async () => {
    const p = fakePreview(
      doc("refused", {
        reason: { code: "aeri_active", text: "the server's sentence" },
        blocked_by: ["aeri_active"],
      }),
    );
    const h = await open(p);
    expect(q(h, "marvel0-preview")?.dataset.state).toBe("refused");
    expect(q(h, "marvel0-preview-reason")?.textContent).toBe(
      en["marvel0.preview.reason.aeri_active"],
    );
    expect(openButton(h)?.disabled).toBe(true);
  });

  test("not the product: the reason is written beside the button, in both languages", async () => {
    for (const language of [false, true]) {
      const p = fakePreview(
        doc("refused", {
          reason: { code: "not_product", text: "only the product" },
          blocked_by: ["not_product"],
        }),
      );
      const h = await open(p, { zhLanguage: language });
      const code = "marvel0.preview.reason.not_product" as const;
      expect(q(h, "marvel0-preview-reason")?.textContent).toBe(
        (language ? zh : en)[code],
      );
      expect(openButton(h)?.disabled).toBe(true);
    }
  });

  test("a start the server refuses is shown as a refusal in words", async () => {
    const p = fakePreview();
    p.s.startAnswer = refused(409, "client_connected", {
      reasons: ["client_connected"],
    });
    const h = await open(p);
    await click(openButton(h));
    const block = q(h, "marvel0-preview-refused")!;
    expect(block.textContent).toContain(
      en["marvel0.preview.reason.client_connected"],
    );
    expect(block.getAttribute("role")).toBe("alert");
    expect(img(h, "side")).toBeNull();
    expect(openButton(h)?.disabled).toBe(false); // (the next read decides)
  });

  test("a start with no answer says so; the write may have been carried out", async () => {
    const p = fakePreview();
    p.s.startAnswer = nothing();
    const h = await open(p);
    await click(openButton(h));
    expect(q(h, "marvel0-preview-refused")?.textContent).toContain(
      en["marvel0.err.network_write"],
    );
  });

  test("a state that cannot be read is said, with no button to press blindly", async () => {
    const p = fakePreview();
    p.s.readAnswer = nothing();
    const h = await open(p);
    expect(q(h, "marvel0-preview-unreadable")?.textContent).toBe(
      en["marvel0.preview.unreadable"],
    );
    expect(openButton(h)).toBeNull();
    expect(stopButton(h)).toBeNull();
  });

  test("a read that fails while the preview holds the cameras leaves Stop on the card", async () => {
    const p = fakePreview(doc("on"));
    const h = await open(p);
    expect(stopButton(h)).not.toBeNull();
    const before = p.s.reads;
    p.s.readAnswer = nothing();
    // the next scheduled read (once a second) fails
    await waitFor(() => p.s.reads > before, { label: "the next read" });
    await settle();
    expect(stopButton(h)).not.toBeNull();
    expect(q(h, "marvel0-preview-unreadable")).toBeNull();
  });

  test("a preview that stopped by itself says why", async () => {
    for (const by of ["idle", "max_lifetime", "client_start", "service"]) {
      const p = fakePreview(doc("off", { stopped_by: by }));
      const h = await open(p);
      expect(q(h, "marvel0-preview-stopped")?.textContent).toBe(
        en[`marvel0.preview.stopped.${by}` as keyof typeof en],
      );
    }
  });
});

describe("a preview the page reads itself", () => {
  test("the card shows the state it is handed and reads nothing of its own", async () => {
    const p = fakePreview(doc("on"));
    const run = fakeRun();
    function Page() {
      // (the page reads it once: the command card must be able to stop it too)
      const control = useCameraPreview(p.api, true, { pollMs: 60_000 });
      return (
        <LiveFrames
          active={false}
          api={run.api}
          preview={p.api}
          control={control}
          pollMs={60_000}
        />
      );
    }
    const view = await render(<Page />);
    await settle();
    await waitFor(
      () => view.host.querySelector("[data-testid=marvel0-preview-stop]"),
      {
        label: "Stop from the handed state",
      },
    );
    expect(p.s.reads).toBe(1); // one reader, not two
    await waitFor(() => view.host.querySelector("img"), { label: "a picture" });
    expect(new Set(p.s.frames)).toEqual(new Set(["side", "wrist"]));
  });
});

describe("when the page can no longer read the preview", () => {
  test("pictures stop being asked for once the answer is too old, so the backend's idle limit lets the cameras go", async () => {
    const realNow = Date.now;
    let skew = 0;
    Date.now = () => realNow() + skew;
    try {
      const p = fakePreview(doc("on"));
      const h = await open(p);
      await waitFor(() => img(h, "side"), { label: "a preview picture" });
      expect(stopButton(h)).not.toBeNull();
      p.s.readAnswer = nothing();
      skew = 60_000; // the last good answer is a minute old at the next read
      await waitFor(() => q(h, "marvel0-preview-unreadable"), {
        label: "the unreadable line",
        timeoutMs: 5_000,
      });
      expect(stopButton(h)).toBeNull();
      await settle();
      const asked = p.s.frames.length;
      await new Promise((resolve) => setTimeout(resolve, 350));
      await settle();
      expect(p.s.frames.length).toBe(asked);
      expect(img(h, "side")).toBeNull();
    } finally {
      Date.now = realNow;
    }
  });
});

describe("when a run is active", () => {
  test("the preview's controls and reads are gone and the run's pictures are shown", async () => {
    const p = fakePreview();
    const h = await open(p, { active: true });
    expect(q(h, "marvel0-preview")).toBeNull();
    expect(openButton(h)).toBeNull();
    expect(p.s.reads).toBe(0);
    expect(p.s.frames).toEqual([]);
    expect(h.run.calls.length).toBeGreaterThan(0);
    expect(h.host.textContent).toContain(en["marvel0.live.lead"]);
    expect(h.host.textContent).not.toContain(en["marvel0.preview.lead"]);
  });

  test("a run that becomes active takes the card over from a running preview", async () => {
    const p = fakePreview(doc("on"));
    const h = await open(p);
    await waitFor(() => img(h, "side"), { label: "a preview picture" });
    const before = { reads: p.s.reads, frames: p.s.frames.length };
    expect(h.run.calls).toEqual([]);
    await h.setActive(true);
    expect(q(h, "marvel0-preview")).toBeNull();
    expect(stopButton(h)).toBeNull();
    // the preview's pictures were let go of; the run's route is asked now
    expect(revoked.length).toBeGreaterThanOrEqual(2);
    await waitFor(() => h.run.calls.length >= 2, { label: "the run's frames" });
    expect(p.s.reads).toBe(before.reads);
    expect(p.s.frames.length).toBe(before.frames);
    // and when the run is over the preview is offered again, from a fresh read
    p.s.doc = doc("off", { stopped_by: "client_start" });
    await h.setActive(false);
    await waitFor(() => openButton(h), { label: "Open again" });
    expect(p.s.reads).toBeGreaterThan(before.reads);
  });

  test("the document from before the run is not shown while the first read after it is out", async () => {
    const p = fakePreview(doc("on"));
    const h = await open(p);
    await waitFor(() => img(h, "side"), { label: "a preview picture" });
    await h.setActive(true);
    // the run came and went; the next read of the preview hangs
    let release: () => void = () => {};
    p.s.hold = new Promise<void>((resolve) => (release = resolve));
    p.s.doc = doc("off", { stopped_by: "client_start" });
    const framesBefore = p.s.frames.length;
    await h.setActive(false);
    await settle();
    expect(q(h, "marvel0-preview-stop")).toBeNull();
    expect(q(h, "marvel0-preview")?.dataset.state).not.toBe("on");
    expect(img(h, "side")).toBeNull();
    expect(p.s.frames.length).toBe(framesBefore); // no picture asked for
    await act(async () => {
      release();
    });
    await waitFor(() => openButton(h), { label: "Open from the fresh read" });
    expect(q(h, "marvel0-preview-stopped")?.textContent).toBe(
      en["marvel0.preview.stopped.client_start"],
    );
  });

  test("no preview picture is shown again when the card comes back", async () => {
    const p = fakePreview(doc("on"));
    const h = await open(p);
    await waitFor(() => img(h, "side"));
    await h.setActive(true);
    p.s.doc = doc("off");
    await h.setActive(false);
    await settle();
    expect(img(h, "side")).toBeNull();
    expect(empty(h, "side")).not.toBeNull();
  });
});
