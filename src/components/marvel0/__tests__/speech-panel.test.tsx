import { render, setupDom } from "@/components/ds/__tests__/dom";
import { act } from "react";
import { afterEach, describe, expect, jest, test } from "bun:test";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import { LocaleProvider } from "@/components/levi-locale";
import { MIC_CHOICE_KEY, MIC_SEEN_KEY } from "../mic-choice";
import { SpeechPanel } from "../speech-panel";
import { button, choose, nothing, refused, type } from "./actions-helpers";
import {
  KNOWN,
  RESTRICTED,
  candidate,
  captureFactory,
  deferred,
  fakeSpeechApi,
  heardAnswer,
  installFakeAudio,
  matchA,
  matchB,
  matchC,
  matchV,
  matchedAnswer,
  ok,
  statusAnswer,
  type FakeAudio,
  type FakeDevice,
  type Script,
} from "./speech-fixtures";

// Bun runs afterEach hooks in registration order. Restore the clock before
// setupDom awaits React unmount; audio/network fakes stay installed until after it.
afterEach(() => jest.useRealTimers());
setupDom();

const ALL_MODEL = "pi05_fr3_all_step49999";
const SP_MODEL = "pi05_fr3_sp_v1";

let audio: FakeAudio | null = null;
afterEach(() => {
  audio?.restore();
  audio = null;
  try {
    localStorage.clear();
  } catch {
    // no storage in this DOM
  }
});

async function flushAll() {
  await act(async () => {
    for (let i = 0; i < 30; i += 1) await Promise.resolve();
  });
}
const advance = (ms: number) =>
  act(async () => {
    jest.advanceTimersByTime(ms);
    for (let i = 0; i < 10; i += 1) await Promise.resolve();
  });

const echo =
  (
    match: Record<string, unknown> | null = matchA(),
    over: Record<string, unknown> = {},
  ) =>
  (call: { requestId: string }) =>
    ok(heardAnswer(call.requestId, match, over));

async function open({
  script = {},
  modelId = ALL_MODEL,
  zhLanguage = false,
  secure = true,
  devices,
}: {
  script?: Script;
  modelId?: string;
  zhLanguage?: boolean;
  secure?: boolean;
  devices?: FakeDevice[];
} = {}) {
  jest.useFakeTimers();
  audio = installFakeAudio({ secure, devices });
  if (zhLanguage) localStorage.setItem("levi-language", "zh");
  const captures = captureFactory();
  const fake = fakeSpeechApi({ transcribe: echo(), ...script });
  const used: string[] = [];
  const element = (id: string) => {
    const panel = (
      <SpeechPanel
        api={fake.api}
        modelId={id}
        onUse={(prompt) => used.push(prompt)}
        createCapture={captures.make}
      />
    );
    return zhLanguage ? <LocaleProvider>{panel}</LocaleProvider> : panel;
  };
  const view = await render(element(modelId));
  await flushAll();
  const h = {
    ...view,
    ...fake,
    captures,
    used,
    audio: audio!,
    setModel: async (id: string) => {
      await view.rerender(element(id));
      await flushAll();
    },
  };
  return h;
}
type H = Awaited<ReturnType<typeof open>>;

const q = (h: H, testid: string) =>
  h.host.querySelector<HTMLElement>(`[data-testid=${testid}]`);
const mic = (h: H) => q(h, "marvel0-speech-mic") as HTMLButtonElement;
const text = (h: H) => h.host.textContent ?? "";
const use = (h: H) => q(h, "marvel0-speech-use") as HTMLButtonElement | null;
const radios = (h: H) =>
  Array.from(h.host.querySelectorAll<HTMLInputElement>("input[type=radio]"));

function fire(target: Element, event: Event) {
  return act(async () => {
    target.dispatchEvent(event);
  });
}
const pointer = (target: Element, type: string, init: PointerEventInit = {}) =>
  fire(
    target,
    new PointerEvent(type, {
      pointerId: 1,
      pointerType: "mouse",
      button: 0,
      bubbles: true,
      cancelable: true,
      ...init,
    }),
  );

/** Holds the talk button for `ms`, lets the clip finish, and answers. */
async function speak(h: H, ms = 400) {
  await pointer(mic(h), "pointerdown");
  await advance(ms);
  await pointer(mic(h), "pointerup");
  await act(async () => h.captures.current.deliver());
  await flushAll();
}

/** Waits out the 800 ms before Use may be pressed. */
const armed = () => advance(800);

describe("the microphone picker", () => {
  const UGREEN = {
    deviceId: "ugreen-1",
    label: "UGREEN USB MIC-CM727 Analog Stereo",
  };
  const CAMERA = {
    deviceId: "insta-1",
    label: "Insta360 Ace Pro 2 Analog Stereo",
  };
  const remember = (device: FakeDevice) =>
    localStorage.setItem(
      MIC_CHOICE_KEY,
      JSON.stringify({ id: device.deviceId, label: device.label }),
    );
  const stored = () => {
    const raw = localStorage.getItem(MIC_CHOICE_KEY);
    return raw ? (JSON.parse(raw) as { id: string; label: string }) : null;
  };
  const select = (h: H) =>
    q(h, "marvel0-speech-mic-select") as HTMLSelectElement;
  const options = (h: H) =>
    Array.from(select(h).options).map((option) => option.textContent);

  test("lists the system default and the connected inputs without opening anything; the default is used", async () => {
    const h = await open({ devices: [CAMERA, UGREEN] });
    expect(options(h)).toEqual([
      "Default (the browser picks)",
      CAMERA.label,
      UGREEN.label,
    ]);
    expect(select(h).value).toBe("");
    expect(h.audio.getUserMediaCalls).toBe(0);
    expect(h.captures.current.device).toBeNull();
    expect(text(h)).toContain(en["marvel0.speech.mic.pick.hint"]);
    expect(q(h, "marvel0-speech-mic-missing")).toBeNull();
  });

  test("a choice goes to the capture and is remembered in this browser, never sent to LEVI", async () => {
    const h = await open({ devices: [CAMERA, UGREEN] });
    await choose(select(h), UGREEN.deviceId);
    await flushAll();
    expect(select(h).value).toBe(UGREEN.deviceId);
    expect(h.captures.current.device).toBe(UGREEN.deviceId);
    expect(stored()).toEqual({ id: UGREEN.deviceId, label: UGREEN.label });
    expect(JSON.stringify(h.calls)).not.toContain(UGREEN.deviceId);
    expect(JSON.stringify(h.calls)).not.toContain("UGREEN");
    // Back to the default: the choice is forgotten.
    await choose(select(h), "");
    await flushAll();
    expect(h.captures.current.device).toBeNull();
    expect(stored()).toBeNull();
  });

  test("a remembered input is used when the page opens", async () => {
    remember(UGREEN);
    const h = await open({ devices: [CAMERA, UGREEN] });
    expect(select(h).value).toBe(UGREEN.deviceId);
    expect(h.captures.current.device).toBe(UGREEN.deviceId);
  });

  test("a remembered input that is unplugged: the default is used and the card says so; plugged back, it is used again", async () => {
    remember(UGREEN);
    const h = await open({ devices: [CAMERA] });
    expect(select(h).value).toBe("");
    expect(h.captures.current.device).toBeNull();
    expect(q(h, "marvel0-speech-mic-missing")?.textContent).toContain(
      UGREEN.label,
    );
    expect(stored()?.id).toBe(UGREEN.deviceId);
    await act(async () => h.audio.setDevices([CAMERA, UGREEN]));
    await flushAll();
    expect(q(h, "marvel0-speech-mic-missing")).toBeNull();
    expect(select(h).value).toBe(UGREEN.deviceId);
    expect(h.captures.current.device).toBe(UGREEN.deviceId);
    // Unplugged during the session: back to the default at once.
    await act(async () => h.audio.setDevices([CAMERA]));
    await flushAll();
    expect(h.captures.current.device).toBeNull();
    expect(q(h, "marvel0-speech-mic-missing")).not.toBeNull();
  });

  test("an input found again under a new id is the same microphone, and its new id is remembered", async () => {
    remember(UGREEN);
    const moved = { deviceId: "ugreen-2", label: UGREEN.label };
    const h = await open({ devices: [CAMERA, moved] });
    expect(select(h).value).toBe("ugreen-2");
    expect(h.captures.current.device).toBe("ugreen-2");
    expect(stored()).toEqual({ id: "ugreen-2", label: UGREEN.label });
    expect(q(h, "marvel0-speech-mic-missing")).toBeNull();
  });

  test("names kept back until the microphone is allowed: the default, a hint, and the names after the first opening", async () => {
    const h = await open({ devices: [{ deviceId: "", label: "" }] });
    expect(options(h)).toEqual(["Default (the browser picks)"]);
    expect(text(h)).toContain(en["marvel0.speech.mic.pick.hidden"]);
    h.audio.setDevices([CAMERA, UGREEN], false);
    const before = h.audio.enumerateCalls;
    await act(async () =>
      h.captures.current.opened({ id: CAMERA.deviceId, label: CAMERA.label }),
    );
    await flushAll();
    expect(h.audio.enumerateCalls).toBe(before + 1);
    expect(options(h)).toEqual([
      "Default (the browser picks)",
      CAMERA.label,
      UGREEN.label,
    ]);
    expect(text(h)).not.toContain(en["marvel0.speech.mic.pick.hidden"]);
  });

  test("a remembered input stays selected while the names are kept back", async () => {
    remember(UGREEN);
    const h = await open({ devices: [{ deviceId: "", label: "" }] });
    expect(options(h)).toEqual(["Default (the browser picks)", UGREEN.label]);
    expect(select(h).value).toBe(UGREEN.deviceId);
    expect(h.captures.current.device).toBe(UGREEN.deviceId);
  });

  test("says which input the last opening used, and warns when the default replaced the chosen one", async () => {
    const h = await open({ devices: [CAMERA, UGREEN] });
    await act(async () =>
      h.captures.current.opened({ id: CAMERA.deviceId, label: CAMERA.label }),
    );
    await flushAll();
    expect(q(h, "marvel0-speech-mic-in-use")?.textContent).toContain(
      CAMERA.label,
    );
    await act(async () =>
      h.captures.current.opened({
        id: CAMERA.deviceId,
        label: CAMERA.label,
        fallback: true,
      }),
    );
    await flushAll();
    expect(q(h, "marvel0-speech-mic-fallback")?.textContent).toContain(
      CAMERA.label,
    );
    expect(q(h, "marvel0-speech-mic-in-use")).toBeNull();
    // A new choice clears what the old input said.
    await choose(select(h), UGREEN.deviceId);
    await flushAll();
    expect(q(h, "marvel0-speech-mic-fallback")).toBeNull();
  });

  test("is locked while recording", async () => {
    const h = await open({ devices: [CAMERA, UGREEN] });
    expect(select(h).disabled).toBe(false);
    await pointer(mic(h), "pointerdown");
    expect(select(h).disabled).toBe(true);
    await advance(400);
    await pointer(mic(h), "pointerup");
    await act(async () => h.captures.current.cancel());
    await flushAll();
    expect(select(h).disabled).toBe(false);
  });

  test("is locked while a recording is being sent", async () => {
    const pending = deferred<ReturnType<typeof ok>>();
    const h = await open({
      devices: [CAMERA, UGREEN],
      script: { transcribe: () => pending.promise },
    });
    await speak(h);
    expect(h.host.querySelector("[data-phase=uploading]")).not.toBeNull();
    expect(select(h).disabled).toBe(true);
    pending.resolve(ok(heardAnswer(h.calls.transcribe[0].requestId, matchA())));
    await flushAll();
    expect(select(h).disabled).toBe(false);
  });

  test("a capture made again (new props) is given the chosen input too", async () => {
    remember(UGREEN);
    const h = await open({ devices: [CAMERA, UGREEN] });
    const first = h.captures.current;
    const other = captureFactory();
    await h.rerender(
      <SpeechPanel
        api={h.api}
        modelId={ALL_MODEL}
        onUse={() => undefined}
        createCapture={other.make}
      />,
    );
    await flushAll();
    expect(first.disposed).toBe(true);
    expect(other.current.device).toBe(UGREEN.deviceId);
  });

  test("a missing input can be forgotten: the default from now on, even when it comes back", async () => {
    remember(UGREEN);
    const h = await open({ devices: [CAMERA] });
    const forget = q(h, "marvel0-speech-mic-forget") as HTMLButtonElement;
    expect(forget.textContent).toBe(en["marvel0.speech.mic.pick.forget"]);
    await act(async () => forget.click());
    await flushAll();
    expect(stored()).toBeNull();
    expect(q(h, "marvel0-speech-mic-missing")).toBeNull();
    expect(q(h, "marvel0-speech-mic-forget")).toBeNull();
    expect(select(h).value).toBe("");
    await act(async () => h.audio.setDevices([CAMERA, UGREEN]));
    await flushAll();
    expect(h.captures.current.device).toBeNull();
  });

  test("what the last opening used is cleared once that input is unplugged", async () => {
    remember(UGREEN);
    const h = await open({ devices: [CAMERA, UGREEN] });
    await act(async () =>
      h.captures.current.opened({ id: UGREEN.deviceId, label: UGREEN.label }),
    );
    await flushAll();
    expect(q(h, "marvel0-speech-mic-in-use")).not.toBeNull();
    await act(async () => h.audio.setDevices([CAMERA]));
    await flushAll();
    expect(q(h, "marvel0-speech-mic-in-use")).toBeNull();
    expect(q(h, "marvel0-speech-mic-missing")).not.toBeNull();
  });

  test("its notes appear inside one status region that is there before any note", async () => {
    remember(UGREEN);
    const h = await open({ devices: [CAMERA, UGREEN] });
    const region = q(h, "marvel0-speech-mic-status")!;
    expect(region.getAttribute("role")).toBe("status");
    expect(region.textContent).toBe("");
    await act(async () => h.audio.setDevices([CAMERA]));
    await flushAll();
    expect(q(h, "marvel0-speech-mic-status")).toBe(region);
    expect(
      region.querySelector("[data-testid=marvel0-speech-mic-missing]"),
    ).not.toBeNull();
    expect(region.querySelectorAll("[role=status]")).toHaveLength(0);
  });

  test("is not shown on a page that cannot capture, and lists nothing there", async () => {
    const h = await open({ secure: false, devices: [CAMERA] });
    expect(q(h, "marvel0-speech-mic-pick")).toBeNull();
    expect(h.audio.enumerateCalls).toBe(0);
  });

  test("speaks Chinese", async () => {
    const h = await open({ devices: [CAMERA], zhLanguage: true });
    expect(options(h)).toEqual(["默认（由浏览器决定）", CAMERA.label]);
    expect(text(h)).toContain(zh["marvel0.speech.mic.pick.label"]);
    expect(text(h)).toContain(zh["marvel0.speech.mic.pick.hint"]);
  });
});

describe("new and changed microphones", () => {
  const UGREEN = {
    deviceId: "ugreen-1",
    label: "UGREEN USB MIC-CM727 Analog Stereo",
  };
  const CAMERA = {
    deviceId: "insta-1",
    label: "Insta360 Ace Pro 2 Analog Stereo",
  };
  const NEWMIC = { deviceId: "rode-1", label: "RODE NT-USB Mini" };
  const remember = (device: FakeDevice) =>
    localStorage.setItem(
      MIC_CHOICE_KEY,
      JSON.stringify({ id: device.deviceId, label: device.label }),
    );
  const seen = (...devices: FakeDevice[]) =>
    localStorage.setItem(
      MIC_SEEN_KEY,
      JSON.stringify(devices.map((device) => `name:${device.label}`)),
    );
  const stored = () => {
    const raw = localStorage.getItem(MIC_CHOICE_KEY);
    return raw ? (JSON.parse(raw) as { id: string; label: string }) : null;
  };
  const select = (h: H) =>
    q(h, "marvel0-speech-mic-select") as HTMLSelectElement;
  const all = (h: H, testid: string) =>
    Array.from(h.host.querySelectorAll<HTMLElement>(`[data-testid=${testid}]`));
  const plug = async (h: H, devices: FakeDevice[]) => {
    await act(async () => h.audio.setDevices(devices));
    await flushAll();
  };

  test("the first list a browser sees is only recorded: nothing offered, nothing switched", async () => {
    const h = await open({ devices: [CAMERA, UGREEN] });
    expect(all(h, "marvel0-speech-mic-offer")).toHaveLength(0);
    expect(q(h, "marvel0-speech-mic-switched")).toBeNull();
    expect(JSON.parse(localStorage.getItem(MIC_SEEN_KEY)!)).toEqual([
      `name:${CAMERA.label}`,
      `name:${UGREEN.label}`,
    ]);
    expect(h.captures.current.device).toBeNull();
  });

  test("a swap found when the page opens: the new microphone is used and remembered, and the card says so", async () => {
    remember(UGREEN);
    seen(CAMERA, UGREEN);
    const h = await open({ devices: [CAMERA, NEWMIC] });
    expect(h.captures.current.device).toBe(NEWMIC.deviceId);
    expect(select(h).value).toBe(NEWMIC.deviceId);
    expect(stored()).toEqual({
      id: NEWMIC.deviceId,
      label: NEWMIC.label,
      replaced: { id: UGREEN.deviceId, label: UGREEN.label },
    });
    const note = q(h, "marvel0-speech-mic-switched")?.textContent ?? "";
    expect(note).toContain(NEWMIC.label);
    expect(note).toContain(UGREEN.label);
    expect(q(h, "marvel0-speech-mic-missing")).toBeNull();
    // Undo: the default from now on.
    await act(async () =>
      (q(h, "marvel0-speech-mic-forget") as HTMLButtonElement).click(),
    );
    await flushAll();
    expect(h.captures.current.device).toBeNull();
    expect(stored()).toBeNull();
    expect(q(h, "marvel0-speech-mic-switched")).toBeNull();
    expect(document.activeElement).toBe(select(h));
  });

  test("a swap while the page is open: unplug the old one, plug in the new one", async () => {
    remember(UGREEN);
    seen(CAMERA, UGREEN);
    const h = await open({ devices: [CAMERA, UGREEN] });
    expect(h.captures.current.device).toBe(UGREEN.deviceId);
    await plug(h, [CAMERA]);
    expect(q(h, "marvel0-speech-mic-missing")).not.toBeNull();
    await plug(h, [CAMERA, NEWMIC]);
    expect(h.captures.current.device).toBe(NEWMIC.deviceId);
    expect(q(h, "marvel0-speech-mic-switched")).not.toBeNull();
    expect(q(h, "marvel0-speech-mic-missing")).toBeNull();
  });

  test("an extra microphone while the chosen one is connected is offered, not taken", async () => {
    remember(UGREEN);
    seen(CAMERA, UGREEN);
    const h = await open({ devices: [CAMERA, UGREEN] });
    await plug(h, [CAMERA, UGREEN, NEWMIC]);
    expect(h.captures.current.device).toBe(UGREEN.deviceId);
    expect(
      all(h, "marvel0-speech-mic-offer").map((element) => element.textContent),
    ).toEqual([`New microphone found: ${NEWMIC.label}.`]);
    const take = q(h, "marvel0-speech-mic-use-new") as HTMLButtonElement;
    expect(take.textContent).toBe(`Use ${NEWMIC.label}`);
    await act(async () => take.click());
    await flushAll();
    expect(h.captures.current.device).toBe(NEWMIC.deviceId);
    expect(stored()?.id).toBe(NEWMIC.deviceId);
    expect(all(h, "marvel0-speech-mic-offer")).toHaveLength(0);
    expect(document.activeElement).toBe(select(h));
  });

  test("an offer can be dismissed, is not repeated after a reload, and goes when the microphone is unplugged", async () => {
    seen(CAMERA, UGREEN);
    const h = await open({ devices: [CAMERA, UGREEN, NEWMIC] });
    expect(all(h, "marvel0-speech-mic-offer")).toHaveLength(1);
    await act(async () =>
      (q(h, "marvel0-speech-mic-dismiss") as HTMLButtonElement).click(),
    );
    await flushAll();
    expect(all(h, "marvel0-speech-mic-offer")).toHaveLength(0);
    expect(h.captures.current.device).toBeNull();
    // It is seen now: plugging it out and in again offers nothing.
    await plug(h, [CAMERA, UGREEN]);
    await plug(h, [CAMERA, UGREEN, NEWMIC]);
    expect(all(h, "marvel0-speech-mic-offer")).toHaveLength(0);
  });

  test("an offered microphone that is unplugged is no longer offered", async () => {
    seen(CAMERA, UGREEN);
    const h = await open({ devices: [CAMERA, UGREEN, NEWMIC] });
    expect(all(h, "marvel0-speech-mic-offer")).toHaveLength(1);
    await plug(h, [CAMERA, UGREEN]);
    expect(all(h, "marvel0-speech-mic-offer")).toHaveLength(0);
    expect(q(h, "marvel0-speech-mic-use-new")).toBeNull();
  });

  test("a very quiet recording is still sent, and the card says how to raise the level", async () => {
    const h = await open({ devices: [CAMERA, UGREEN] });
    await pointer(mic(h), "pointerdown");
    await advance(400);
    await pointer(mic(h), "pointerup");
    await act(async () => h.captures.current.deliver({ peakDb: -38.4 }));
    await flushAll();
    expect(h.calls.transcribe).toHaveLength(1);
    expect(q(h, "marvel0-speech-mic-quiet")?.textContent).toContain(
      "peak -38 dBFS",
    );
    await armed();
    await advance(60_000);
    await pointer(mic(h), "pointerdown");
    await advance(400);
    await pointer(mic(h), "pointerup");
    await act(async () => h.captures.current.deliver({ peakDb: -8 }));
    await flushAll();
    expect(q(h, "marvel0-speech-mic-quiet")).toBeNull();
  });

  test("after a swap, the microphone chosen before is used again once it is back", async () => {
    remember(UGREEN);
    seen(CAMERA, UGREEN);
    const h = await open({ devices: [CAMERA, UGREEN] });
    await plug(h, [CAMERA]);
    await plug(h, [CAMERA, NEWMIC]);
    expect(h.captures.current.device).toBe(NEWMIC.deviceId);
    // The stand-in goes too: its absence is reported, nothing is lost.
    await plug(h, [CAMERA]);
    expect(h.captures.current.device).toBeNull();
    // The person's own microphone comes back.
    await plug(h, [CAMERA, UGREEN]);
    expect(h.captures.current.device).toBe(UGREEN.deviceId);
    expect(stored()).toEqual({ id: UGREEN.deviceId, label: UGREEN.label });
    expect(q(h, "marvel0-speech-mic-restored")?.textContent).toContain(
      UGREEN.label,
    );
    expect(q(h, "marvel0-speech-mic-missing")).toBeNull();
    expect(q(h, "marvel0-speech-mic-switched")).toBeNull();
  });

  test("a second swap still names, and goes back to, the person's own microphone", async () => {
    const HEADSET = { deviceId: "hs-1", label: "Someone's USB Headset" };
    const YETI = { deviceId: "yeti-1", label: "Blue Yeti" };
    remember(UGREEN);
    seen(CAMERA, UGREEN);
    const h = await open({ devices: [CAMERA, UGREEN] });
    await plug(h, [CAMERA]);
    await plug(h, [CAMERA, HEADSET]);
    expect(h.captures.current.device).toBe(HEADSET.deviceId);
    await plug(h, [CAMERA]);
    await plug(h, [CAMERA, YETI]);
    expect(h.captures.current.device).toBe(YETI.deviceId);
    const note = q(h, "marvel0-speech-mic-switched")?.textContent ?? "";
    expect(note).toContain(UGREEN.label);
    expect(note).not.toContain(HEADSET.label);
    expect(stored()?.replaced).toEqual({
      id: UGREEN.deviceId,
      label: UGREEN.label,
    });
    await plug(h, [CAMERA, YETI, UGREEN]);
    expect(h.captures.current.device).toBe(UGREEN.deviceId);
    // Unplugged again: "used again" no longer stands next to "not connected".
    await plug(h, [CAMERA, YETI]);
    expect(q(h, "marvel0-speech-mic-missing")).not.toBeNull();
    expect(q(h, "marvel0-speech-mic-restored")).toBeNull();
  });

  test("the microphone chosen before is used again even while the stand-in is still connected", async () => {
    remember(UGREEN);
    seen(CAMERA, UGREEN);
    const h = await open({ devices: [CAMERA, NEWMIC] });
    expect(h.captures.current.device).toBe(NEWMIC.deviceId);
    await plug(h, [CAMERA, NEWMIC, UGREEN]);
    expect(h.captures.current.device).toBe(UGREEN.deviceId);
    expect(all(h, "marvel0-speech-mic-offer")).toHaveLength(0);
  });

  test("a choice of the person's after a swap is final: the old microphone coming back changes nothing", async () => {
    remember(UGREEN);
    seen(CAMERA, UGREEN);
    const h = await open({ devices: [CAMERA, NEWMIC] });
    await choose(select(h), CAMERA.deviceId);
    await flushAll();
    expect(stored()).toEqual({ id: CAMERA.deviceId, label: CAMERA.label });
    await plug(h, [CAMERA, NEWMIC, UGREEN]);
    expect(h.captures.current.device).toBe(CAMERA.deviceId);
    expect(q(h, "marvel0-speech-mic-restored")).toBeNull();
  });

  test("plugging the new microphone in first, then unplugging the old one, also switches", async () => {
    remember(UGREEN);
    seen(CAMERA, UGREEN);
    const h = await open({ devices: [CAMERA, UGREEN] });
    await plug(h, [CAMERA, UGREEN, NEWMIC]);
    expect(all(h, "marvel0-speech-mic-offer")).toHaveLength(1);
    expect(h.captures.current.device).toBe(UGREEN.deviceId);
    await plug(h, [CAMERA, NEWMIC]);
    expect(h.captures.current.device).toBe(NEWMIC.deviceId);
    expect(q(h, "marvel0-speech-mic-switched")).not.toBeNull();
    expect(all(h, "marvel0-speech-mic-offer")).toHaveLength(0);
  });

  test("the very-quiet note goes when the next recording starts", async () => {
    const h = await open({ devices: [CAMERA, UGREEN] });
    await pointer(mic(h), "pointerdown");
    await advance(400);
    await pointer(mic(h), "pointerup");
    await act(async () => h.captures.current.deliver({ peakDb: -40 }));
    await flushAll();
    expect(q(h, "marvel0-speech-mic-quiet")).not.toBeNull();
    await armed();
    await advance(60_000);
    await pointer(mic(h), "pointerdown");
    await flushAll();
    expect(q(h, "marvel0-speech-mic-quiet")).toBeNull();
  });

  test("a fallback warning stays when the chosen microphone is found again during that opening", async () => {
    // Names kept back at load, the remembered one connected under a new id:
    // the exact id fails, the default opens, then the names reveal the move.
    remember(UGREEN);
    const moved = { deviceId: "ugreen-2", label: UGREEN.label };
    const h = await open({ devices: [{ deviceId: "", label: "" }] });
    h.audio.setDevices([CAMERA, moved], false);
    await act(async () =>
      h.captures.current.opened({
        id: CAMERA.deviceId,
        label: CAMERA.label,
        fallback: true,
      }),
    );
    await flushAll();
    expect(h.captures.current.device).toBe("ugreen-2");
    expect(q(h, "marvel0-speech-mic-fallback")?.textContent).toContain(
      CAMERA.label,
    );
  });
});

describe("what the card always says", () => {
  const SENTENCE =
    "Voice cannot stop or pause the robot. Use the physical e-stop.";
  const ZH_SENTENCE = "语音不能让机械臂停下或暂停，请按物理急停。";

  test("the e-stop sentence is in both catalogues, word for word", () => {
    expect(en["marvel0.speech.no_estop"]).toBe(SENTENCE);
    expect(zh["marvel0.speech.no_estop"]).toBe(ZH_SENTENCE);
  });

  test("the e-stop sentence stays on the card in every state", async () => {
    const pending = deferred<ReturnType<typeof ok>>();
    const h = await open({ script: { transcribe: () => pending.promise } });
    const here = () => q(h, "marvel0-speech-estop")?.textContent ?? "";
    expect(here()).toContain(SENTENCE); // idle
    await pointer(mic(h), "pointerdown");
    expect(h.host.querySelector("[data-phase=recording]")).not.toBeNull();
    expect(here()).toContain(SENTENCE); // recording
    await advance(400);
    await pointer(mic(h), "pointerup");
    await act(async () => h.captures.current.deliver());
    expect(h.host.querySelector("[data-phase=uploading]")).not.toBeNull();
    expect(here()).toContain(SENTENCE); // uploading
    pending.resolve(ok(heardAnswer(h.calls.transcribe[0].requestId, matchA())));
    await flushAll();
    expect(h.host.querySelector("[data-phase=proposal]")).not.toBeNull();
    expect(here()).toContain(SENTENCE); // proposal
  });

  test("it stays when the card is off, unreadable or without a microphone, and in Chinese", async () => {
    const closed = await open({
      script: {
        status: ok(
          statusAnswer({ gate: { allowed: false, reasons: ["arm_in_use"] } }),
        ),
      },
    });
    expect(text(closed)).toContain(SENTENCE);
    const lost = await open({ script: { status: refused(404, "http_404") } });
    expect(text(lost)).toContain(SENTENCE);
    const insecure = await open({ secure: false });
    expect(text(insecure)).toContain(SENTENCE);
    const chinese = await open({ zhLanguage: true });
    expect(text(chinese)).toContain(ZH_SENTENCE);
  });

  test("no sentence of the card says the page listens", async () => {
    const h = await open();
    await speak(h);
    const lines = Object.entries(en).filter(([key]) =>
      key.startsWith("marvel0.speech."),
    );
    expect(lines.length).toBeGreaterThan(50);
    for (const [key, value] of lines)
      expect(value, key).not.toMatch(/listen|hear.*command/i);
    expect(text(h)).not.toMatch(/listening/i);
  });
});

describe("opening the card", () => {
  test("refreshes passive state and asks for nothing else: no microphone, no clip, no write", async () => {
    const h = await open();
    await advance(120_000);
    expect(h.calls.status).toBe(5);
    expect(h.calls.transcribe).toHaveLength(0);
    expect(h.calls.match).toHaveLength(0);
    expect(h.calls.release).toBe(0);
    expect(h.audio.getUserMediaCalls).toBe(0);
    expect(mic(h).disabled).toBe(false);
  });

  test("a closed gate switches the card off and says why; a press starts nothing", async () => {
    const h = await open({
      script: {
        status: ok(
          statusAnswer({
            gate: {
              allowed: false,
              reasons: ["client_connected", "gpu_insufficient"],
            },
          }),
        ),
      },
    });
    expect(mic(h).disabled).toBe(true);
    expect(text(h)).toContain(en["marvel0.speech.gate.client_connected"]);
    expect(text(h)).toContain(en["marvel0.speech.gate.gpu_insufficient"]);
    await pointer(mic(h), "pointerdown");
    await advance(500);
    expect(h.captures.current.starts).toBe(0);
    expect(h.audio.getUserMediaCalls).toBe(0);
  });

  test("an unavailable state switches it off with the server's reason in words", async () => {
    const h = await open({
      script: {
        status: ok(
          statusAnswer({
            state: "unavailable",
            reason: { code: "speech_manifest_mismatch", text: "files differ" },
          }),
        ),
      },
    });
    expect(mic(h).disabled).toBe(true);
    expect(text(h)).toContain(en["marvel0.err.speech.manifest_mismatch"]);
  });

  test("a voice input that is not installed gets a one-line install hint", async () => {
    const h = await open({
      script: {
        status: ok(
          statusAnswer({
            state: "unavailable",
            installed: { worker: false, model: false },
          }),
        ),
      },
    });
    expect(mic(h).disabled).toBe(true);
    expect(q(h, "marvel0-speech-install")?.textContent).toBe(
      en["marvel0.speech.install"],
    );
    expect(en["marvel0.speech.install"]).toContain("integrations/stt/setup.sh");
  });

  test("a state that cannot be read keeps it off, and Check again reads it once more", async () => {
    let answer = refused(503, "site_missing");
    const h = await open({ script: { status: () => answer } });
    expect(mic(h).disabled).toBe(true);
    expect(text(h)).toContain(en["marvel0.speech.status_failed"]);
    answer = ok(statusAnswer()) as never;
    await act(async () => button(h.host, "Check again")!.click());
    await flushAll();
    expect(h.calls.status).toBe(2);
    expect(mic(h).disabled).toBe(false);
  });

  test("a page that is not a secure context says where to open LEVI and never reaches for the microphone", async () => {
    const h = await open({ secure: false });
    expect(mic(h).disabled).toBe(true);
    expect(text(h)).toContain("http://127.0.0.1:7860");
    expect(text(h)).toContain("http://localhost:7860");
    expect(text(h)).toContain(en["marvel0.speech.mic.insecure"]);
    await pointer(mic(h), "pointerdown");
    expect(h.audio.getUserMediaCalls).toBe(0);
  });

  test("speaks the reasons in Chinese when the page is Chinese", async () => {
    const h = await open({
      zhLanguage: true,
      script: {
        status: ok(
          statusAnswer({ gate: { allowed: false, reasons: ["arm_in_use"] } }),
        ),
      },
    });
    await flushAll();
    expect(text(h)).toContain(zh["marvel0.speech.gate.arm_in_use"]);
    expect(text(h)).toContain(zh["marvel0.speech.title"]);
  });
});

describe("a recording", () => {
  test("says what is happening while it holds, then sends one clip as audio/wav for the model of the form", async () => {
    const h = await open();
    await pointer(mic(h), "pointerdown");
    expect(mic(h).getAttribute("aria-pressed")).toBe("true");
    expect(q(h, "marvel0-speech-status")?.textContent).toBe(
      en["marvel0.speech.state.recording"],
    );
    await advance(400);
    await pointer(mic(h), "pointerup");
    await act(async () => h.captures.current.deliver());
    await flushAll();
    expect(h.calls.transcribe).toHaveLength(1);
    const call = h.calls.transcribe[0];
    expect(call.modelId).toBe(ALL_MODEL);
    expect(call.requestId).toMatch(/^[A-Za-z0-9._:-]{8,80}$/);
    expect(call.clip.type).toBe("audio/wav");
    expect(h.audio.getUserMediaCalls).toBe(0); // the fake capture asked for nothing
  });

  test("a press while the microphone is being opened says to allow it", async () => {
    const h = await open();
    await act(async () =>
      h.captures.current.events.onState?.("requesting", "idle"),
    );
    expect(q(h, "marvel0-speech-status")?.textContent).toBe(
      en["marvel0.speech.state.opening"],
    );
  });

  test("a problem of the microphone is said in its own words, with the addresses LEVI must be opened under", async () => {
    const h = await open();
    await pointer(mic(h), "pointerdown");
    await act(async () => h.captures.current.fail("denied"));
    const problem = q(h, "marvel0-speech-problem")!;
    expect(problem.getAttribute("data-kind")).toBe("denied");
    expect(problem.textContent).toContain(en["marvel0.speech.mic.denied"]);
    expect(problem.textContent).toContain("http://127.0.0.1:7860");
    await act(async () => h.captures.current.fail("busy"));
    expect(q(h, "marvel0-speech-problem")?.textContent).toContain(
      en["marvel0.speech.mic.busy"],
    );
    expect(q(h, "marvel0-speech-proposal")).toBeNull();
  });

  test("a result without speech is a notice with its reason, not an error and not a proposal", async () => {
    for (const reason of [
      "no_speech",
      "language",
      "low_confidence",
      "empty",
      "too_short",
    ]) {
      const h = await open({
        script: {
          transcribe: (call) =>
            ok(
              heardAnswer(call.requestId, null, {
                heard: false,
                reason,
                transcript: "",
                language: null,
              }),
            ),
        },
      });
      await speak(h);
      const notice = q(h, "marvel0-speech-notice")!;
      expect(notice.textContent, reason).toBe(
        en[`marvel0.speech.heard.${reason}` as keyof typeof en],
      );
      expect(q(h, "marvel0-speech-proposal")).toBeNull();
      expect(q(h, "marvel0-speech-problem")).toBeNull();
    }
  });

  test("a refusal of the server is said in words and the state is read again", async () => {
    const h = await open({
      script: {
        transcribe: () =>
          refused(409, "speech_gpu_insufficient", { free_mib: 100 }),
      },
    });
    await speak(h);
    expect(q(h, "marvel0-speech-problem")?.textContent).toContain(
      en["marvel0.err.speech.gpu_insufficient"],
    );
    expect(h.calls.status).toBe(2);
    expect(q(h, "marvel0-speech-proposal")).toBeNull();
  });

  test("busy, no answer, and an answer in a format it does not know each say so", async () => {
    const busy = await open({
      script: { transcribe: () => refused(429, "speech_busy") },
    });
    await speak(busy);
    expect(text(busy)).toContain(en["marvel0.err.speech.busy"]);
    const lost = await open({
      script: { transcribe: () => nothing("fetch failed") },
    });
    await speak(lost);
    expect(text(lost)).toContain(en["marvel0.err.network"]);
    const odd = await open({
      script: { transcribe: () => ok({ hello: "world" }) },
    });
    await speak(odd);
    expect(text(odd)).toContain(en["marvel0.err.speech.format"]);
    expect(q(odd, "marvel0-speech-proposal")).toBeNull();
  });

  test("an answer to another request id is not shown", async () => {
    const h = await open({
      script: {
        transcribe: () => ok(heardAnswer("req-someone-else", matchA())),
      },
    });
    await speak(h);
    expect(q(h, "marvel0-speech-proposal")).toBeNull();
    expect(text(h)).toContain(en["marvel0.err.speech.format"]);
  });

  test("a recording that the 10 s limit ended says the end may be missing", async () => {
    const h = await open();
    await pointer(mic(h), "pointerdown");
    await act(async () => h.captures.current.deliver({ truncated: true }));
    await flushAll();
    expect(text(h)).toContain("Recording stopped by itself at 10 s");
  });

  test("the talk button draws the level bar from the capture's samples", async () => {
    const h = await open();
    await pointer(mic(h), "pointerdown");
    await act(async () =>
      h.captures.current.events.onLevel?.({ rmsDb: -30, peakDb: -30 }),
    );
    await advance(50);
    const bar = h.host.querySelector<HTMLElement>(".rg-speech__meter-bar")!;
    expect(bar.style.getPropertyValue("--rg-level")).toBe("0.500");
  });
});

describe("the proposal", () => {
  test("tier A: one sentence in <bdi>, the transcript with its language and model, Use only after 800 ms", async () => {
    const h = await open({
      script: {
        transcribe: echo(
          matchA(KNOWN[2], {
            best: candidate(KNOWN[2], {
              fixes: [{ heard: "egplant", as: "eggplant" }],
            }),
          }),
          { transcript: "pick the egplant in the blue plate" },
        ),
      },
    });
    await speak(h);
    const proposal = q(h, "marvel0-speech-proposal")!;
    expect(proposal.getAttribute("data-view")).toBe("A");
    const best = q(h, "marvel0-speech-best")!;
    expect(best.querySelector("bdi")?.textContent).toBe(KNOWN[2]);
    expect(best.getAttribute("lang")).toBe("en");
    const heard = q(h, "marvel0-speech-transcript")!;
    expect(heard.querySelector("bdi")?.textContent).toBe(
      "pick the egplant in the blue plate",
    );
    expect(heard.getAttribute("lang")).toBe("en");
    expect(proposal.textContent).toContain(
      "Heard (English, qwen3-asr-1.7b-hf):",
    );
    expect(proposal.textContent).toContain("Read “egplant” as “eggplant”.");
    expect(button(h.host, "Use this instruction")).toBeTruthy();
    expect(button(h.host, "Not this one")).toBeTruthy();
    expect(radios(h)).toHaveLength(0);
    // Use is off, and presses do nothing, until the proposal has been on screen
    expect(use(h)!.disabled).toBe(true);
    await advance(799);
    expect(use(h)!.disabled).toBe(true);
    await act(async () => use(h)!.click());
    expect(h.used).toEqual([]);
    await advance(1);
    expect(use(h)!.disabled).toBe(false);
  });

  test("Use hands over the allowed sentence once, says that nothing has started, and leaves no proposal", async () => {
    const h = await open({
      script: {
        transcribe: echo(matchA(KNOWN[2]), {
          transcript: "pick the eggplant in the blue plate please",
        }),
      },
    });
    await speak(h);
    await armed();
    await act(async () => use(h)!.click());
    expect(h.used).toEqual([KNOWN[2]]);
    expect(h.used[0]).not.toContain("please");
    expect(q(h, "marvel0-speech-proposal")).toBeNull();
    expect(use(h)).toBeNull();
    const notice = q(h, "marvel0-speech-notice")!;
    expect(notice.getAttribute("data-kind")).toBe("used");
    expect(notice.textContent).toContain("Nothing has started");
    expect(notice.textContent).toContain(en["marvel0.cmd.btn.make"]);
    // nothing else was asked of the server
    expect(h.calls.match).toHaveLength(0);
    expect(h.calls.transcribe).toHaveLength(1);
  });

  test("Use is not the focus, and a key that ends the recording does not press it", async () => {
    const h = await open();
    mic(h).focus();
    await fire(
      mic(h),
      new KeyboardEvent("keydown", {
        key: " ",
        bubbles: true,
        cancelable: true,
      }),
    );
    await advance(400);
    await fire(
      mic(h),
      new KeyboardEvent("keyup", { key: " ", bubbles: true, cancelable: true }),
    );
    await act(async () => h.captures.current.deliver());
    await flushAll();
    expect(use(h)).not.toBeNull();
    expect(document.activeElement).toBe(mic(h));
    for (const key of [" ", "Enter"]) {
      await fire(
        use(h)!,
        new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true }),
      );
      await fire(
        use(h)!,
        new KeyboardEvent("keyup", { key, bubbles: true, cancelable: true }),
      );
    }
    await act(async () => use(h)!.click());
    expect(h.used).toEqual([]);
  });

  test("expires after 60 s and cannot be used then", async () => {
    const h = await open();
    await speak(h);
    await armed();
    await advance(60_000 - 800 - 1);
    expect(q(h, "marvel0-speech-proposal")).not.toBeNull();
    expect(use(h)!.disabled).toBe(false);
    await advance(1);
    expect(q(h, "marvel0-speech-proposal")).toBeNull();
    expect(q(h, "marvel0-speech-notice")?.textContent).toBe(
      en["marvel0.speech.expired"],
    );
    expect(h.used).toEqual([]);
  });

  test("a new recording takes the proposal away at once", async () => {
    const h = await open();
    await speak(h);
    await armed();
    expect(use(h)).not.toBeNull();
    await pointer(mic(h), "pointerdown");
    expect(q(h, "marvel0-speech-proposal")).toBeNull();
    expect(use(h)).toBeNull();
  });

  test("a pending upload cannot be replaced by a second recording", async () => {
    const pending = deferred<ReturnType<typeof ok>>();
    const h = await open({ script: { transcribe: () => pending.promise } });
    await speak(h);
    const starts = h.captures.current.starts;
    expect(mic(h).disabled).toBe(true);
    await pointer(mic(h), "pointerdown");
    await pointer(mic(h), "pointerup");
    expect(h.captures.current.starts).toBe(starts);
    expect(h.calls.transcribe).toHaveLength(1);
    expect(h.calls.transcribe[0].signal?.aborted).toBe(false);
    pending.resolve(ok(heardAnswer(h.calls.transcribe[0].requestId, matchA())));
    await flushAll();
    expect(mic(h).disabled).toBe(false);
    expect(q(h, "marvel0-speech-best")?.textContent).toBe(KNOWN[2]);
  });

  test("switching the model revalidates the proposal against the new model's list", async () => {
    const pending = deferred<ReturnType<typeof ok>>();
    const h = await open({
      script: {
        transcribe: echo(matchA(KNOWN[2])),
        match: () => pending.promise,
      },
    });
    await speak(h);
    await armed();
    expect(use(h)!.disabled).toBe(false);
    await h.setModel(SP_MODEL);
    // asked again, with the new model, and frozen meanwhile
    expect(h.calls.match).toHaveLength(1);
    expect(h.calls.match[0]).toMatchObject({
      modelId: SP_MODEL,
      transcript: "pick the eggplant in the blue plate",
    });
    expect(use(h)!.disabled).toBe(true);
    expect(q(h, "marvel0-speech-proposal")?.getAttribute("aria-busy")).toBe(
      "true",
    );
    expect(q(h, "marvel0-speech-status")?.textContent).toBe(
      en["marvel0.speech.state.checking"],
    );
    // the restricted model knows two sentences, and the eggplant is not one of them
    pending.resolve(
      ok(matchedAnswer(matchC({ family: "restricted", allowed: RESTRICTED }))),
    );
    await flushAll();
    expect(q(h, "marvel0-speech-proposal")?.getAttribute("data-view")).toBe(
      "list",
    );
    expect(radios(h).map((r) => r.value)).toEqual(RESTRICTED);
    expect(q(h, "marvel0-speech-best")).toBeNull();
    expect(text(h)).toContain(en["marvel0.speech.family.restricted"]);
    // the new proposal has to be read again before it can be used
    expect(use(h)!.disabled).toBe(true);
  });

  test("a model switched while the clip is on its way gets the new model's match", async () => {
    const pending = deferred<ReturnType<typeof ok>>();
    const h = await open({
      script: {
        transcribe: () => pending.promise,
        match: () =>
          ok(
            matchedAnswer(
              matchC({ family: "restricted", allowed: RESTRICTED }),
            ),
          ),
      },
    });
    await speak(h);
    await h.setModel(SP_MODEL);
    pending.resolve(
      ok(heardAnswer(h.calls.transcribe[0].requestId, matchA(KNOWN[2]))),
    );
    await flushAll();
    expect(h.calls.match).toHaveLength(1);
    expect(h.calls.match[0].modelId).toBe(SP_MODEL);
    expect(radios(h).map((r) => r.value)).toEqual(RESTRICTED);
    expect(q(h, "marvel0-speech-best")).toBeNull();
  });
});

describe("the tiers", () => {
  test("A: 'Not this one' shows the whole list with nothing chosen", async () => {
    const h = await open();
    await speak(h);
    await armed();
    await act(async () => button(h.host, "Not this one")!.click());
    expect(q(h, "marvel0-speech-proposal")?.getAttribute("data-view")).toBe(
      "list",
    );
    expect(radios(h).map((r) => r.value)).toEqual(KNOWN);
    expect(radios(h).some((r) => r.checked)).toBe(false);
    expect(use(h)!.disabled).toBe(true);
    await act(async () => radios(h)[0].click());
    expect(use(h)!.disabled).toBe(false);
    await act(async () => use(h)!.click());
    expect(h.used).toEqual([KNOWN[0]]);
  });

  test("B: three choices, none chosen, 'Use selected' only after one is picked", async () => {
    const h = await open({ script: { transcribe: echo(matchB()) } });
    await speak(h);
    const view = q(h, "marvel0-speech-proposal")!;
    expect(view.getAttribute("data-tier")).toBe("B");
    expect(radios(h)).toHaveLength(3);
    expect(radios(h).map((r) => r.value)).toEqual([
      KNOWN[1],
      KNOWN[0],
      KNOWN[2],
    ]);
    expect(radios(h).some((r) => r.checked)).toBe(false);
    expect(button(h.host, "Use this instruction")).toBeUndefined();
    expect(q(h, "marvel0-speech-differences")?.textContent).toContain(
      "Colour: you said “red”",
    );
    await armed();
    expect(use(h)!.disabled).toBe(true); // nothing picked
    await act(async () => radios(h)[2].click());
    expect(radios(h)[2].checked).toBe(true);
    expect(use(h)!.disabled).toBe(false);
    await act(async () => use(h)!.click());
    expect(h.used).toEqual([KNOWN[2]]);
  });

  test("B: 'None of these' shows the whole allowed list", async () => {
    const h = await open({
      script: {
        transcribe: echo(
          matchB({ allowed: [...KNOWN, "put the red screw in the box"] }),
        ),
      },
    });
    await speak(h);
    expect(radios(h)).toHaveLength(3);
    await act(async () => q(h, "marvel0-speech-none")!.click());
    expect(radios(h)).toHaveLength(4);
  });

  test("C: the whole allowed list, nothing chosen, and the hint", async () => {
    const h = await open({ script: { transcribe: echo(matchC()) } });
    await speak(h);
    expect(q(h, "marvel0-speech-proposal")?.getAttribute("data-view")).toBe(
      "list",
    );
    expect(radios(h).map((r) => r.value)).toEqual(KNOWN);
    expect(radios(h).some((r) => r.checked)).toBe(false);
    expect(text(h)).toContain(en["marvel0.speech.c.hint"]);
    expect(button(h.host, "Use this instruction")).toBeUndefined();
  });

  for (const flag of ["stop", "negation", "unsafe"] as const)
    test(`V (${flag}): a banner, the transcript as text, and no candidate to use`, async () => {
      const h = await open({
        script: {
          transcribe: echo(matchV(flag), { transcript: "stop right now" }),
        },
      });
      await speak(h);
      await armed();
      const banner = q(h, "marvel0-speech-flag")!;
      expect(banner.getAttribute("data-flag")).toBe(flag);
      expect(banner.textContent).toContain(
        en[`marvel0.speech.flag.${flag}.title` as keyof typeof en],
      );
      expect(banner.querySelector("[role=alert]")).not.toBeNull();
      expect(radios(h)).toHaveLength(0);
      expect(use(h)).toBeNull();
      expect(button(h.host, /Use/)).toBeUndefined();
      for (const prompt of KNOWN) expect(text(h)).not.toContain(prompt);
      expect(q(h, "marvel0-speech-transcript")?.textContent).toBe(
        "stop right now",
      );
      expect(h.used).toEqual([]);
    });

  test("a stop banner says again that voice does not stop the robot", async () => {
    const h = await open({ script: { transcribe: echo(matchV("stop")) } });
    await speak(h);
    expect(q(h, "marvel0-speech-flag")?.textContent).toContain(
      "Voice cannot stop or pause the robot",
    );
    expect(en["marvel0.speech.flag.stop.body"]).toContain("physical e-stop");
  });

  test("an empty allowed list says so and offers nothing", async () => {
    const h = await open({
      script: { transcribe: echo(matchC({ allowed: [] })) },
    });
    await speak(h);
    expect(radios(h)).toHaveLength(0);
    expect(text(h)).toContain(en["marvel0.speech.empty_list"]);
    await armed();
    expect(use(h)!.disabled).toBe(true);
  });
});

describe("the transcript", () => {
  test("is shown as text, not markup, with invisible and bidi controls removed", async () => {
    const h = await open({
      script: {
        transcribe: echo(matchC(), {
          transcript: "<img src=x onerror=alert(1)> pick‮ the​ plate",
        }),
      },
    });
    await speak(h);
    const heard = q(h, "marvel0-speech-transcript")!;
    expect(heard.querySelector("img")).toBeNull();
    expect(heard.querySelector("bdi")?.textContent).toBe(
      "<img src=x onerror=alert(1)> pick the plate",
    );
    expect(heard.textContent).not.toMatch(/[‮​]/);
  });

  test("can be edited and matched again: the text goes to the match route, never into Use", async () => {
    const h = await open({
      script: {
        transcribe: echo(matchC(), { transcript: "pik the egplant" }),
        match: () => ok(matchedAnswer(matchA(KNOWN[2]))),
      },
    });
    await speak(h);
    await act(async () => button(h.host, "Edit the text")!.click());
    const input = h.host.querySelector<HTMLInputElement>(
      "input[data-speech-draft]",
    )!;
    expect(input.value).toBe("pik the egplant");
    await type(input, "pick the eggplant");
    await act(async () => button(h.host, "Match again")!.click());
    await flushAll();
    expect(h.calls.match).toEqual([
      expect.objectContaining({
        transcript: "pick the eggplant",
        modelId: ALL_MODEL,
      }),
    ]);
    expect(text(h)).toContain(en["marvel0.speech.edited"]);
    expect(q(h, "marvel0-speech-best")?.textContent).toBe(KNOWN[2]);
    await armed();
    await act(async () => use(h)!.click());
    expect(h.used).toEqual([KNOWN[2]]); // the sentence of the list, not the typed text
  });

  test("Enter in the text to match matches again and is cancelled, so no form around it submits", async () => {
    const h = await open({
      script: {
        transcribe: echo(matchC(), { transcript: "pik the egplant" }),
        match: () => ok(matchedAnswer(matchC())),
      },
    });
    await speak(h);
    await act(async () => button(h.host, "Edit the text")!.click());
    const input = h.host.querySelector<HTMLInputElement>(
      "input[data-speech-draft]",
    )!;
    await type(input, "pick");
    const enter = new KeyboardEvent("keydown", {
      key: "Enter",
      bubbles: true,
      cancelable: true,
    });
    await fire(input, enter);
    await flushAll();
    expect(enter.defaultPrevented).toBe(true);
    expect(h.calls.match).toHaveLength(1);
    // and Enter on a radio of the list does not submit either
    const radio = radios(h)[0];
    const onRadio = new KeyboardEvent("keydown", {
      key: "Enter",
      bubbles: true,
      cancelable: true,
    });
    await fire(radio, onRadio);
    expect(onRadio.defaultPrevented).toBe(true);
  });

  test("a failed match says so and drops the proposal", async () => {
    const h = await open({
      script: {
        transcribe: echo(matchC(), { transcript: "pik" }),
        match: () => refused(502, "speech_failed"),
      },
    });
    await speak(h);
    await act(async () => button(h.host, "Edit the text")!.click());
    await act(async () => button(h.host, "Match again")!.click());
    await flushAll();
    // the draft equals the transcript: nothing to match, the editor closes
    expect(h.calls.match).toHaveLength(0);
    expect(h.host.querySelector("input[data-speech-draft]")).toBeNull();
    await act(async () => button(h.host, "Edit the text")!.click());
    const input = h.host.querySelector<HTMLInputElement>(
      "input[data-speech-draft]",
    )!;
    await type(input, "pick");
    await act(async () => button(h.host, "Match again")!.click());
    await flushAll();
    expect(q(h, "marvel0-speech-problem")?.textContent).toContain(
      en["marvel0.err.speech.failed"],
    );
    expect(q(h, "marvel0-speech-proposal")).toBeNull();
  });
});

describe("in Chinese", () => {
  test("the tiers, the differences and the notices use the Chinese catalogue", async () => {
    const h = await open({
      zhLanguage: true,
      script: {
        transcribe: echo(matchB(), {
          language: "Chinese",
          transcript: "把盘子叠起来",
        }),
      },
    });
    await speak(h);
    const view = q(h, "marvel0-speech-proposal")!;
    expect(view.textContent).toContain(zh["marvel0.speech.b.title"]);
    expect(view.textContent).toContain(
      "听到的内容（中文，qwen3-asr-1.7b-hf）：",
    );
    expect(view.textContent).toContain(
      "颜色：你说的是“red”，指令里是“same color”。",
    );
    expect(q(h, "marvel0-speech-transcript")?.getAttribute("lang")).toBe("zh");
    expect(button(h.host, zh["marvel0.speech.btn.use_selected"])).toBeTruthy();
  });
});

describe("leaving", () => {
  test("unmounting aborts what is on its way and ends the capture", async () => {
    const pending = deferred<ReturnType<typeof ok>>();
    const h = await open({ script: { transcribe: () => pending.promise } });
    await speak(h);
    const capture = h.captures.current;
    await act(async () => h.rerender(<div />));
    expect(h.calls.transcribe[0].signal?.aborted).toBe(true);
    expect(capture.disposed).toBe(true);
    pending.resolve(ok(heardAnswer(h.calls.transcribe[0].requestId, matchA())));
    await flushAll(); // nothing is rendered, and nothing throws
  });

  test("the mic control exists exactly once on the card, and nowhere inside a dialog", async () => {
    const h = await open();
    expect(
      h.host.querySelectorAll("[data-testid=marvel0-speech-mic]"),
    ).toHaveLength(1);
    expect(
      h.host.querySelectorAll("button .lucide-mic, button svg").length,
    ).toBeGreaterThan(0);
    expect(
      mic(h).closest("dialog, [role=dialog], [aria-modal=true]"),
    ).toBeNull();
  });

  test("Free the speech model unloads it and reads the state again; it shows only while the model is loaded", async () => {
    const off = await open();
    expect(button(off.host, en["marvel0.speech.btn.release"])).toBeUndefined();
    const h = await open({
      script: { status: ok(statusAnswer({ state: "ready" })) },
    });
    await act(async () =>
      button(h.host, en["marvel0.speech.btn.release"])!.click(),
    );
    await flushAll();
    expect(h.calls.release).toBe(1);
    expect(h.calls.status).toBe(2);
  });
});

describe("freshness at Use", () => {
  for (const tier of ["A", "B"] as const) {
    test(`${tier}: rejecting during Use revokes its delayed response and permits a new choice`, async () => {
      const oldCheck = deferred<ReturnType<typeof ok>>();
      const newCheck = deferred<ReturnType<typeof ok>>();
      let check = 0;
      const h = await open({
        script: {
          transcribe: echo(tier === "A" ? matchA() : matchB()),
          status: () =>
            check === 1
              ? oldCheck.promise
              : check === 2
                ? newCheck.promise
                : ok(statusAnswer()),
        },
      });
      await speak(h);
      await armed();
      if (tier === "B") await act(async () => radios(h)[0].click());
      check = 1;
      await act(async () => use(h)!.click());
      const reject = q(
        h,
        tier === "A" ? "marvel0-speech-not-this" : "marvel0-speech-none",
      ) as HTMLButtonElement;
      expect(reject.disabled).toBe(false);
      await act(async () => reject.click());
      expect(q(h, "marvel0-speech-tier-list")).not.toBeNull();
      expect(
        radios(h).every((radio) => !radio.checked && !radio.disabled),
      ).toBe(true);
      expect(use(h)!.disabled).toBe(true);
      await act(async () => radios(h)[0].click());
      expect(use(h)!.disabled).toBe(false);
      check = 2;
      await act(async () => use(h)!.click());
      oldCheck.resolve(ok(statusAnswer()));
      await flushAll();
      expect(h.used).toEqual([]);
      expect(use(h)!.disabled).toBe(true);
      newCheck.resolve(ok(statusAnswer()));
      await flushAll();
      expect(h.used).toEqual([KNOWN[0]]);
    });
  }

  test("a changed allowed revision in the same model expires without filling", async () => {
    let revision = "test-revision";
    const h = await open({
      script: {
        status: () => ok(statusAnswer({ allowed_revision: revision })),
      },
    });
    await speak(h);
    await armed();
    revision = "changed-revision";
    await act(async () => use(h)!.click());
    await flushAll();
    expect(h.used).toEqual([]);
    expect(use(h)).toBeNull();
    expect(text(h)).toContain(en["marvel0.speech.expired"]);
  });

  test("a gate that closes after the proposal cannot fill the form", async () => {
    let allowed = true;
    const h = await open({
      script: {
        status: () =>
          ok(
            statusAnswer({
              gate: { allowed, reasons: allowed ? [] : ["policy_starting"] },
            }),
          ),
      },
    });
    await speak(h);
    await armed();
    allowed = false;
    await act(async () => use(h)!.click());
    await flushAll();
    expect(h.used).toEqual([]);
    expect(mic(h).disabled).toBe(true);
  });

  test("a formerly closed gate refreshes while the page stays visible", async () => {
    let allowed = false;
    const h = await open({
      script: {
        status: () =>
          ok(
            statusAnswer({
              gate: { allowed, reasons: allowed ? [] : ["policy_starting"] },
            }),
          ),
      },
    });
    expect(mic(h).disabled).toBe(true);
    allowed = true;
    await advance(30_000);
    await flushAll();
    expect(mic(h).disabled).toBe(false);
    expect(h.audio.getUserMediaCalls).toBe(0);
    expect(h.calls.transcribe).toEqual([]);
  });

  test("Use revalidation is single-flight and a pending check cannot start a mic", async () => {
    const pending = deferred<ReturnType<typeof ok>>();
    let verify = false;
    const h = await open({
      script: { status: () => (verify ? pending.promise : ok(statusAnswer())) },
    });
    await speak(h);
    await armed();
    verify = true;
    const before = h.calls.status;
    await act(async () => {
      use(h)!.click();
      use(h)!.click();
    });
    expect(h.calls.status).toBe(before + 1);
    expect(mic(h).disabled).toBe(true);
    const starts = h.captures.current.starts;
    await pointer(mic(h), "pointerdown");
    expect(h.captures.current.starts).toBe(starts);
    pending.resolve(ok(statusAnswer()));
    await flushAll();
    expect(h.used).toEqual([KNOWN[2]]);
  });

  test("IME Enter commits input without matching or submitting", async () => {
    const h = await open();
    await speak(h);
    await act(async () =>
      button(h.host, en["marvel0.speech.btn.edit"])!.click(),
    );
    const input = h.host.querySelector<HTMLInputElement>(
      "input[data-speech-draft=true]",
    )!;
    expect(input).not.toBeNull();
    const event = new KeyboardEvent("keydown", {
      key: "Enter",
      isComposing: true,
      bubbles: true,
      cancelable: true,
    });
    await fire(input, event);
    expect(h.calls.match).toEqual([]);
    expect(event.defaultPrevented).toBe(false);
  });
});

test("the existing live region announces when a proposal arrives", async () => {
  const h = await open();
  const live = q(h, "marvel0-speech-status")!;
  expect(live.getAttribute("role")).toBe("status");
  expect(live.textContent).toBe("");
  await speak(h);
  expect(q(h, "marvel0-speech-status")).toBe(live);
  expect(live.textContent).toBe(en["marvel0.speech.review_ready"]);
});
