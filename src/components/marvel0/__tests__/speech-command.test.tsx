// The voice card inside the Client command card, with the real capture over a
// fake browser audio stack and a fake network: the whole way from a press to a
// filled task instruction, and what must NOT happen on the way.
import { render, setupDom } from "@/components/ds/__tests__/dom";
import { act } from "react";
import {
  afterAll,
  afterEach,
  beforeAll,
  describe,
  expect,
  jest,
  test,
} from "bun:test";
import { readModels, readSite } from "../catalog-logic";
import { CommandPanel } from "../command-panel";
import { Marvel0Actions } from "../marvel0-actions";
import { NO_CHOICE, effectiveSelection, type Effective } from "../selection";
import type { Marvel0Catalog } from "../use-marvel0-actions";
import { modelsAnswer, siteAnswer, model } from "./catalog-fixtures";
import { preview } from "./command-fixtures";
import { button, fakeApi, field, ok, type } from "./actions-helpers";
import {
  KNOWN,
  RESTRICTED,
  heardAnswer,
  installFakeAudio,
  matchA,
  matchC,
  matchedAnswer,
  statusAnswer,
  type FakeAudio,
} from "./speech-fixtures";

// Keep one clock throughout this file, including effect cleanup.
// Restoring and reinstalling Bun timers between tests resets performance.now().
// Restore real time before setupDom unregisters the browser globals.
beforeAll(() => jest.useFakeTimers());
afterAll(() => jest.useRealTimers());
// Effects dispose their resources synchronously; verify that before restoring
// audio and fetch. Keep rendering and interactions asynchronous.
setupDom({ unmount: "sync" });

const ALL = "pi05_fr3_all_step49999";
const SP = "pi05_fr3_sp_v1";
const speechModels = () =>
  readModels(
    modelsAnswer({
      models: [
        model(),
        model({ id: SP, config: "pi05_fr3_sp", restricted_prompts: true }),
      ],
    }),
  );
const catalog = (): Marvel0Catalog => ({
  loaded: true,
  models: speechModels(),
  site: readSite(siteAnswer()),
  modelsProblem: null,
  siteProblem: null,
  reload: () => {},
});

type Seen = { path: string; init?: RequestInit };
let audio: FakeAudio | null = null;
let restoreFetch = () => {};
const clipboard: string[] = [];
let pendingStatus: { signal: AbortSignal | null; aborts: number } | null = null;
afterEach(() => {
  try {
    expect(document.body.childElementCount).toBe(0);
    for (const stream of audio?.streams ?? [])
      expect(stream.track.readyState).toBe("ended");
    for (const context of audio?.contexts ?? []) {
      expect(context.state).toBe("closed");
      if (context.workletNode) {
        expect(context.workletNode.connected).toBe(false);
        expect(context.workletNode.port.onmessage).toBeNull();
      }
    }
    if (pendingStatus) {
      expect(pendingStatus.signal?.aborted).toBe(true);
      expect(pendingStatus.aborts).toBe(1);
    }
  } finally {
    try {
      audio?.restore();
    } finally {
      audio = null;
      restoreFetch();
      restoreFetch = () => {};
      clipboard.length = 0;
      pendingStatus = null;
    }
  }
});

async function flushAll() {
  await act(async () => {
    for (let i = 0; i < 40; i += 1) await Promise.resolve();
  });
}
const advance = (ms: number) =>
  act(async () => {
    jest.advanceTimersByTime(ms);
    for (let i = 0; i < 10; i += 1) await Promise.resolve();
  });

/** The core as the voice card meets it. `heard` is the match the next clip gets. */
function network(
  heard: () => Record<string, unknown> = () => matchA(KNOWN[1]),
) {
  const seen: Seen[] = [];
  const original = globalThis.fetch;
  globalThis.fetch = (async (input: unknown, init?: RequestInit) => {
    const path = String(input);
    seen.push({ path, init });
    const json = (body: unknown) =>
      new Response(JSON.stringify(body), { status: 200 });
    if (path.split("?")[0] === "/api/levi/marvel0/speech") {
      if (pendingStatus) {
        const pending = pendingStatus;
        pending.signal = init?.signal ?? null;
        return await new Promise<Response>((_resolve, reject) => {
          pending.signal?.addEventListener(
            "abort",
            () => {
              pending.aborts += 1;
              reject(new DOMException("aborted", "AbortError"));
            },
            { once: true },
          );
        });
      }
      return json(statusAnswer());
    }
    if (path.startsWith("/api/levi/marvel0/speech/transcribe")) {
      const id = new URL(path, "http://127.0.0.1").searchParams.get(
        "request_id",
      )!;
      return json(heardAnswer(id, heard(), { transcript: "stack the plates" }));
    }
    if (path === "/api/levi/marvel0/speech/match")
      return json(matchedAnswer(heard()));
    return new Response("{}", { status: 404 });
  }) as typeof fetch;
  restoreFetch = () => {
    globalThis.fetch = original;
  };
  return seen;
}

async function open(heard?: () => Record<string, unknown>) {
  audio = installFakeAudio();
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText: async (value: string) => void clipboard.push(value) },
  });
  const seen = network(heard);
  const { api, calls } = fakeApi({ preview: ok(preview()) });
  const models = speechModels();
  // The page's one selection (chosen in the policy server card): the form's
  // model follows it.
  const panel = (selection: Effective) => (
    <CommandPanel
      api={api}
      catalog={catalog()}
      status={null}
      selection={selection}
      onUseAnother={() => {}}
    />
  );
  const view = await render(
    panel(effectiveSelection(models, NO_CHOICE, null)!),
  );
  await flushAll();
  /** Another model selected on the page (the policy server card's control). */
  const select = (modelId: string) =>
    view.rerender(
      panel({ ...effectiveSelection(models, NO_CHOICE, null)!, modelId }),
    );
  return { ...view, seen, calls, select, audio: audio! };
}
type H = Awaited<ReturnType<typeof open>>;

const card = (h: H) =>
  h.host.querySelector<HTMLElement>("[data-testid=marvel0-command]")!;
const mic = (h: H) =>
  h.host.querySelector<HTMLButtonElement>("[data-testid=marvel0-speech-mic]")!;
const use = (h: H) =>
  h.host.querySelector<HTMLButtonElement>("[data-testid=marvel0-speech-use]");
const prompt = (h: H) => field(card(h), "Task instruction") as HTMLInputElement;
const fire = (target: Element, event: Event) =>
  act(async () => {
    target.dispatchEvent(event);
  });
const pointer = (target: Element, type: string) =>
  fire(
    target,
    new PointerEvent(type, {
      pointerId: 1,
      pointerType: "mouse",
      button: 0,
      bubbles: true,
      cancelable: true,
    }),
  );

/** Hold the button for `ms`, then let the 200 ms tail and the upload finish. */
async function speak(h: H, ms = 1000) {
  await pointer(mic(h), "pointerdown");
  await flushAll();
  await act(async () => h.audio.seconds(ms / 1000));
  await advance(ms); // a hold, not a tap
  await pointer(mic(h), "pointerup");
  await act(async () => h.audio.seconds(0.2));
  await advance(200);
  await flushAll();
}
const posts = (h: H) => h.seen.filter((call) => call.init?.method === "POST");

describe("the voice card in the Client command card", () => {
  test("sits above the task instruction field, once, and not in a dialog", async () => {
    const h = await open();
    expect(
      h.host.querySelectorAll("[data-testid=marvel0-speech]"),
    ).toHaveLength(1);
    expect(
      h.host.querySelectorAll("[data-testid=marvel0-speech-mic]"),
    ).toHaveLength(1);
    expect(
      mic(h).closest("dialog, [role=dialog], [aria-modal=true]"),
    ).toBeNull();
    const before = h.host.querySelector("[data-testid=marvel0-speech]")!;
    expect(
      before.compareDocumentPosition(prompt(h)) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  test("opening and waiting reads the voice state and nothing else: no microphone, no write", async () => {
    const h = await open();
    // Each passive read answers before the next 30 s refresh. One 180 s
    // jump would expire those requests before their Promise bodies can settle.
    for (let refresh = 0; refresh < 6; refresh += 1) {
      await advance(30_000);
      await flushAll();
      expect(mic(h).disabled).toBe(false);
      expect(
        h.host.querySelector("[data-testid=marvel0-speech-problem]"),
      ).toBeNull();
    }
    expect(h.seen).toHaveLength(7);
    expect(
      h.seen.every(
        (call) =>
          call.path ===
          "/api/levi/marvel0/speech?model_id=pi05_fr3_all_step49999",
      ),
    ).toBe(true);
    expect(h.seen[0].init?.method).toBeUndefined();
    expect(
      h.seen.every(
        (call) =>
          call.init?.method === undefined && call.init?.body === undefined,
      ),
    ).toBe(true);
    expect(posts(h)).toHaveLength(0);
    expect(h.audio.getUserMediaCalls).toBe(0);
    expect(h.calls.preview).toHaveLength(0);
  });

  test("leaving the card aborts a pending status read without opening the microphone", async () => {
    pendingStatus = { signal: null, aborts: 0 };
    const h = await open();
    expect(h.seen).toHaveLength(1);
    expect(pendingStatus.signal?.aborted).toBe(false);
    expect(pendingStatus.aborts).toBe(0);
    expect(h.audio.getUserMediaCalls).toBe(0);
    expect(posts(h)).toHaveLength(0);
    expect(h.calls.preview).toHaveLength(0);
    // The afterEach assertions observe cancellation after setupDom unmounts.
  });

  test("a press asks for the microphone once; the clip goes up as a 16 kHz mono WAV; Use fills the task instruction and nothing else", async () => {
    const h = await open();
    expect(prompt(h).value).toBe("pick the eggplant in the blue plate");
    await speak(h);
    expect(h.audio.getUserMediaCalls).toBe(1);

    const [upload] = posts(h);
    expect(upload.path).toMatch(
      /^\/api\/levi\/marvel0\/speech\/transcribe\?request_id=[A-Za-z0-9._:-]{8,80}&model_id=pi05_fr3_all_step49999$/,
    );
    expect(upload.init?.headers).toEqual({ "Content-Type": "audio/wav" });
    const body = new Uint8Array(
      await (upload.init!.body as Blob).arrayBuffer(),
    );
    const view = new DataView(body.buffer);
    expect(String.fromCharCode(...body.subarray(0, 4))).toBe("RIFF");
    expect(String.fromCharCode(...body.subarray(8, 16))).toBe("WAVEfmt ");
    expect([
      view.getUint16(20, true),
      view.getUint16(22, true),
      view.getUint32(24, true),
      view.getUint16(34, true),
    ]).toEqual([1, 1, 16000, 16]);
    expect(view.getUint32(40, true)).toBe(body.length - 44);
    expect(body.length).toBeGreaterThan(44 + 16000 * 2); // about 1.2 s

    // the proposal is there, and the field is still what it was
    expect(
      h.host.querySelector("[data-testid=marvel0-speech-best]")?.textContent,
    ).toBe(KNOWN[1]);
    expect(prompt(h).value).toBe("pick the eggplant in the blue plate");
    expect(h.calls.preview).toHaveLength(0);

    await advance(800);
    await act(async () => use(h)!.click());
    expect(prompt(h).value).toBe(KNOWN[1]);
    expect(document.activeElement).toBe(prompt(h));
    // Use is one press: nothing was made, copied or asked of the preview route
    expect(h.calls.preview).toHaveLength(0);
    expect(clipboard).toEqual([]);
    expect(
      h.host.querySelector("[data-testid=marvel0-command-result]"),
    ).toBeNull();
    expect(h.host.textContent).toContain("Nothing has started");

    // the command is made by the existing button, with the sentence in the body
    await act(async () => button(card(h), "Make the command")!.click());
    await flushAll();
    expect(h.calls.preview).toHaveLength(1);
    expect(h.calls.preview[0].prompt).toBe(KNOWN[1]);
    expect(clipboard).toEqual([]);
  });

  test("a second press within the 8 s the microphone is kept does not ask the browser again", async () => {
    const h = await open();
    await speak(h);
    await advance(3000);
    await speak(h);
    expect(h.audio.getUserMediaCalls).toBe(1);
    expect(posts(h)).toHaveLength(2);
    await advance(8000);
    expect(h.audio.streams[0].track.readyState).toBe("ended");
  });

  test("a command that was already made turns stale when Use changes the task instruction, and is not made again", async () => {
    const h = await open();
    await act(async () => button(card(h), "Make the command")!.click());
    await flushAll();
    expect(h.calls.preview).toHaveLength(1);
    await speak(h);
    await advance(800);
    await act(async () => use(h)!.click());
    const result = h.host.querySelector(
      "[data-testid=marvel0-command-result]",
    )!;
    expect(result.getAttribute("data-stale")).toBe("true");
    expect(
      result.querySelector("[data-testid=marvel0-command-text]"),
    ).toBeNull();
    await advance(1000);
    expect(h.calls.preview).toHaveLength(1);
    expect(h.calls.preview).toHaveLength(1);
  });

  test("Enter in the transcript's editor matches again and does not press 'Make the command'", async () => {
    const h = await open(() => matchC());
    await speak(h);
    await act(async () => button(h.host, "Edit the text")!.click());
    const input = h.host.querySelector<HTMLInputElement>(
      "input[data-speech-draft]",
    )!;
    await type(input, "stack plates");
    const enter = new KeyboardEvent("keydown", {
      key: "Enter",
      bubbles: true,
      cancelable: true,
    });
    await fire(input, enter);
    await flushAll();
    expect(enter.defaultPrevented).toBe(true);
    expect(h.calls.preview).toHaveLength(0);
    const matches = h.seen.filter((call) =>
      call.path.endsWith("/speech/match"),
    );
    expect(matches).toHaveLength(1);
    expect(JSON.parse(matches[0].init!.body as string)).toEqual({
      transcript: "stack plates",
      model_id: ALL,
    });
  });

  test("selecting another model on the page revalidates the proposal against it", async () => {
    let restricted = false;
    const h = await open(() =>
      restricted
        ? matchC({ family: "restricted", allowed: RESTRICTED })
        : matchA(KNOWN[2]),
    );
    await speak(h);
    expect(
      h.host.querySelector("[data-testid=marvel0-speech-best]"),
    ).not.toBeNull();
    restricted = true;
    await h.select(SP);
    await flushAll();
    const matches = h.seen.filter((call) =>
      call.path.endsWith("/speech/match"),
    );
    expect(matches).toHaveLength(1);
    expect(JSON.parse(matches[0].init!.body as string).model_id).toBe(SP);
    const radios = Array.from(
      h.host.querySelectorAll<HTMLInputElement>("input[type=radio]"),
    );
    expect(radios.map((r) => r.value)).toEqual(RESTRICTED);
    expect(
      h.host.querySelector("[data-testid=marvel0-speech-best]"),
    ).toBeNull();
    await advance(800);
    await act(async () => radios[0].click());
    await act(async () => use(h)!.click());
    expect(prompt(h).value).toBe(RESTRICTED[0]);
  });
});

describe("the action section of the page", () => {
  test("has one microphone control, outside every dialog, even with the policy server card beside it", async () => {
    audio = installFakeAudio();
    network();
    const { api } = fakeApi();
    const view = await render(<Marvel0Actions api={api} />);
    await flushAll();
    expect(
      view.host.querySelectorAll("[data-testid=marvel0-speech-mic]"),
    ).toHaveLength(1);
    expect(
      view.host.querySelectorAll("[data-testid=marvel0-speech]"),
    ).toHaveLength(1);
    for (const dialog of Array.from(
      view.host.querySelectorAll("dialog, [role=dialog], [aria-modal=true]"),
    ))
      expect(dialog.querySelector("[data-testid=marvel0-speech]")).toBeNull();
    expect(audio.getUserMediaCalls).toBe(0);
  });
});
