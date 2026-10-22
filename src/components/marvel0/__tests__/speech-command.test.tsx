// The voice card inside the Client command card, with the real capture over a
// fake browser audio stack and a fake network: the whole way from a press to a
// filled task instruction, and what must NOT happen on the way.
import { render, setupDom } from "@/components/ds/__tests__/dom";
import { act } from "react";
import * as React from "react";
import nodeTimers from "node:timers";
import {
  afterAll,
  afterEach,
  beforeAll,
  beforeEach,
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

// Temporary, bounded CI diagnostics: synchronous observations only. Preserve
// every await and forward the native immediate once with its original handle.
let traceCase = 0;
let traceLines = 0;
let traceActive = true;
let traceCurrentStage = "module";
type TraceOperation = { id: number; test: number; label: string };
let traceNextOperation = 0;
let traceActiveOperation: TraceOperation | null = null;
let immediateScheduled = 0;
let immediateStarted = 0;
let immediateReturned = 0;
const pendingImmediates = new Map<number, { test: number; stage: string }>();
const reactTraceInternals = (
  React as unknown as {
    __CLIENT_INTERNALS_DO_NOT_USE_OR_WARN_USERS_THEY_CANNOT_UPGRADE?: {
      actQueue?: readonly unknown[] | null;
    };
  }
).__CLIENT_INTERNALS_DO_NOT_USE_OR_WARN_USERS_THEY_CANNOT_UPGRADE;
function traceSpeech(
  event: string,
  detail: Record<string, unknown> = {},
  force = false,
) {
  if (!traceActive || (!force && traceLines >= 900)) return;
  traceLines += 1;
  console.log(
    "SPEECH_CI_TRACE",
    JSON.stringify({
      event,
      sequence: traceLines,
      test: traceCase,
      stage: traceCurrentStage,
      operation: traceActiveOperation,
      // Bun 1.3.10 also virtualizes saved hrtime/Date references: these are
      // clock-state diagnostics, not independent measurements of wall time.
      dateNow: Date.now(),
      performanceNow: performance.now(),
      virtualHrtime: String(process.hrtime.bigint()),
      actQueueLength: reactTraceInternals?.actQueue?.length ?? null,
      scheduled: immediateScheduled,
      started: immediateStarted,
      returned: immediateReturned,
      pending: pendingImmediates.size,
      ...detail,
    }),
  );
}
function traceBegin(label: string): TraceOperation {
  const operation = {
    id: ++traceNextOperation,
    test: traceActiveOperation?.test ?? traceCase,
    label,
  };
  traceActiveOperation = operation;
  traceCurrentStage = label + ":start";
  traceSpeech("stage.start");
  return operation;
}
function traceEnd(operation: TraceOperation) {
  traceActiveOperation = operation;
  traceCurrentStage = operation.label + ":end";
  traceSpeech("stage.end");
}
const originalImmediate = nodeTimers.setImmediate;
const originalImmediateDescriptor = Object.getOwnPropertyDescriptor(
  nodeTimers,
  "setImmediate",
)!;
nodeTimers.setImmediate = function (
  this: unknown,
  callback: (...args: unknown[]) => unknown,
  ...args: unknown[]
) {
  const id = ++immediateScheduled;
  const origin = {
    test: traceActiveOperation?.test ?? traceCase,
    stage: traceCurrentStage,
    operation: traceActiveOperation?.id ?? null,
  };
  pendingImmediates.set(id, origin);
  traceSpeech("immediate.schedule", { immediate: id, origin });
  return Reflect.apply(originalImmediate, this, [
    function (this: unknown, ...callbackArgs: unknown[]) {
      immediateStarted += 1;
      traceSpeech("immediate.run", { immediate: id, origin });
      try {
        return Reflect.apply(callback, this, callbackArgs);
      } finally {
        immediateReturned += 1;
        pendingImmediates.delete(id);
        traceSpeech("immediate.return", { immediate: id, origin });
      }
    },
    ...args,
  ]);
} as typeof nodeTimers.setImmediate;
beforeEach(() => {
  traceCase += 1;
  traceActiveOperation = null;
  traceCurrentStage = "test.begin";
  traceSpeech("test.begin", {}, true);
});
afterEach(() => {
  traceSpeech(
    "test.body-exit",
    {
      pendingOrigins: Array.from(pendingImmediates, ([id, origin]) => ({
        id,
        ...origin,
      })).slice(0, 12),
    },
    true,
  );
});
afterAll(() => {
  traceSpeech("file.end", {}, true);
  traceActive = false;
  Object.defineProperty(
    nodeTimers,
    "setImmediate",
    originalImmediateDescriptor,
  );
});

// Keep one clock throughout this file, including effect cleanup.
// Restoring and reinstalling Bun timers between tests resets performance.now().
// Restore real time before setupDom unregisters the browser globals.
beforeAll(() => {
  traceSpeech(
    "clock.before-fake",
    {
      globalImmediateIsSaved: globalThis.setImmediate === originalImmediate,
      globalTimeoutIsNode: globalThis.setTimeout === nodeTimers.setTimeout,
    },
    true,
  );
  jest.useFakeTimers();
  traceSpeech(
    "clock.after-fake",
    {
      globalImmediateIsSaved: globalThis.setImmediate === originalImmediate,
      globalTimeoutIsNode: globalThis.setTimeout === nodeTimers.setTimeout,
    },
    true,
  );
});
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
      traceSpeech("test.cleanup-restored", {}, true);
    }
  }
});

async function flushAll() {
  const traceAwait1 = traceBegin("act@fa3:99");
  await act(async () => {
    const traceCallback = traceActiveOperation;
    traceSpeech("act.callback.enter", {
      callback: "act@fa3:99",
      operation: traceCallback,
    });

    for (let i = 0; i < 40; i += 1) await Promise.resolve();

    traceActiveOperation = traceCallback;
    traceSpeech("act.callback.return", {
      callback: "act@fa3:99",
      operation: traceCallback,
    });
  });
  traceEnd(traceAwait1);
}
const advance = (ms: number) =>
  act(async () => {
    const traceCallback = traceActiveOperation;
    traceSpeech("act.callback.enter", {
      callback: "act@fa3:104",
      operation: traceCallback,
    });

    jest.advanceTimersByTime(ms);
    for (let i = 0; i < 10; i += 1) await Promise.resolve();

    traceActiveOperation = traceCallback;
    traceSpeech("act.callback.return", {
      callback: "act@fa3:104",
      operation: traceCallback,
    });
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
  const traceAwait2 = traceBegin("render@fa3:173");
  const view = await render(
    panel(effectiveSelection(models, NO_CHOICE, null)!),
  );
  traceEnd(traceAwait2);
  const traceAwait3 = traceBegin("flushAll@fa3:176");
  await flushAll();
  traceEnd(traceAwait3);
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
    const traceCallback = traceActiveOperation;
    traceSpeech("act.callback.enter", {
      callback: "act@fa3:194",
      operation: traceCallback,
    });

    target.dispatchEvent(event);

    traceActiveOperation = traceCallback;
    traceSpeech("act.callback.return", {
      callback: "act@fa3:194",
      operation: traceCallback,
    });
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
  const traceAwait4 = traceBegin("pointer@fa3:211");
  await pointer(mic(h), "pointerdown");
  traceEnd(traceAwait4);
  const traceAwait5 = traceBegin("flushAll@fa3:212");
  await flushAll();
  traceEnd(traceAwait5);
  const traceAwait6 = traceBegin("act@fa3:213");
  await act(async () => {
    const traceCallback = traceActiveOperation;
    traceSpeech("act.callback.enter", {
      callback: "act@fa3:213",
      operation: traceCallback,
    });
    const diagnosticResult = h.audio.seconds(ms / 1000);
    traceActiveOperation = traceCallback;
    traceSpeech("act.callback.return", {
      callback: "act@fa3:213",
      operation: traceCallback,
    });
    return diagnosticResult;
  });
  traceEnd(traceAwait6);
  const traceAwait7 = traceBegin("advance@fa3:214");
  await advance(ms);
  traceEnd(traceAwait7); // a hold, not a tap
  const traceAwait8 = traceBegin("pointer@fa3:215");
  await pointer(mic(h), "pointerup");
  traceEnd(traceAwait8);
  const traceAwait9 = traceBegin("act@fa3:216");
  await act(async () => {
    const traceCallback = traceActiveOperation;
    traceSpeech("act.callback.enter", {
      callback: "act@fa3:216",
      operation: traceCallback,
    });
    const diagnosticResult = h.audio.seconds(0.2);
    traceActiveOperation = traceCallback;
    traceSpeech("act.callback.return", {
      callback: "act@fa3:216",
      operation: traceCallback,
    });
    return diagnosticResult;
  });
  traceEnd(traceAwait9);
  const traceAwait10 = traceBegin("advance@fa3:217");
  await advance(200);
  traceEnd(traceAwait10);
  const traceAwait11 = traceBegin("flushAll@fa3:218");
  await flushAll();
  traceEnd(traceAwait11);
}
const posts = (h: H) => h.seen.filter((call) => call.init?.method === "POST");

describe("the voice card in the Client command card", () => {
  test("sits above the task instruction field, once, and not in a dialog", async () => {
    const traceAwait12 = traceBegin("open@fa3:224");
    const h = await open();
    traceEnd(traceAwait12);
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
    const traceAwait13 = traceBegin("open@fa3:242");
    const h = await open();
    traceEnd(traceAwait13);
    // Each passive read answers before the next 30 s refresh. One 180 s
    // jump would expire those requests before their Promise bodies can settle.
    for (let refresh = 0; refresh < 6; refresh += 1) {
      const traceAwait14 = traceBegin("advance@fa3:246");
      await advance(30_000);
      traceEnd(traceAwait14);
      const traceAwait15 = traceBegin("flushAll@fa3:247");
      await flushAll();
      traceEnd(traceAwait15);
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
    const traceAwait16 = traceBegin("open@fa3:275");
    const h = await open();
    traceEnd(traceAwait16);
    expect(h.seen).toHaveLength(1);
    expect(pendingStatus.signal?.aborted).toBe(false);
    expect(pendingStatus.aborts).toBe(0);
    expect(h.audio.getUserMediaCalls).toBe(0);
    expect(posts(h)).toHaveLength(0);
    expect(h.calls.preview).toHaveLength(0);
    // The afterEach assertions observe cancellation after setupDom unmounts.
  });

  test("a press asks for the microphone once; the clip goes up as a 16 kHz mono WAV; Use fills the task instruction and nothing else", async () => {
    const traceAwait17 = traceBegin("open@fa3:286");
    const h = await open();
    traceEnd(traceAwait17);
    expect(prompt(h).value).toBe("pick the eggplant in the blue plate");
    const traceAwait18 = traceBegin("speak@fa3:288");
    await speak(h);
    traceEnd(traceAwait18);
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

    const traceAwait19 = traceBegin("advance@fa3:318");
    await advance(800);
    traceEnd(traceAwait19);
    const traceAwait20 = traceBegin("act@fa3:319");
    await act(async () => {
      const traceCallback = traceActiveOperation;
      traceSpeech("act.callback.enter", {
        callback: "act@fa3:319",
        operation: traceCallback,
      });
      const diagnosticResult = use(h)!.click();
      traceActiveOperation = traceCallback;
      traceSpeech("act.callback.return", {
        callback: "act@fa3:319",
        operation: traceCallback,
      });
      return diagnosticResult;
    });
    traceEnd(traceAwait20);
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
    const traceAwait21 = traceBegin("act@fa3:331");
    await act(async () => {
      const traceCallback = traceActiveOperation;
      traceSpeech("act.callback.enter", {
        callback: "act@fa3:331",
        operation: traceCallback,
      });
      const diagnosticResult = button(card(h), "Make the command")!.click();
      traceActiveOperation = traceCallback;
      traceSpeech("act.callback.return", {
        callback: "act@fa3:331",
        operation: traceCallback,
      });
      return diagnosticResult;
    });
    traceEnd(traceAwait21);
    const traceAwait22 = traceBegin("flushAll@fa3:332");
    await flushAll();
    traceEnd(traceAwait22);
    expect(h.calls.preview).toHaveLength(1);
    expect(h.calls.preview[0].prompt).toBe(KNOWN[1]);
    expect(clipboard).toEqual([]);
  });

  test("a second press within the 8 s the microphone is kept does not ask the browser again", async () => {
    const traceAwait23 = traceBegin("open@fa3:339");
    const h = await open();
    traceEnd(traceAwait23);
    const traceAwait24 = traceBegin("speak@fa3:340");
    await speak(h);
    traceEnd(traceAwait24);
    const traceAwait25 = traceBegin("advance@fa3:341");
    await advance(3000);
    traceEnd(traceAwait25);
    const traceAwait26 = traceBegin("speak@fa3:342");
    await speak(h);
    traceEnd(traceAwait26);
    expect(h.audio.getUserMediaCalls).toBe(1);
    expect(posts(h)).toHaveLength(2);
    const traceAwait27 = traceBegin("advance@fa3:345");
    await advance(8000);
    traceEnd(traceAwait27);
    expect(h.audio.streams[0].track.readyState).toBe("ended");
  });

  test("a command that was already made turns stale when Use changes the task instruction, and is not made again", async () => {
    const traceAwait28 = traceBegin("open@fa3:350");
    const h = await open();
    traceEnd(traceAwait28);
    const traceAwait29 = traceBegin("act@fa3:351");
    await act(async () => {
      const traceCallback = traceActiveOperation;
      traceSpeech("act.callback.enter", {
        callback: "act@fa3:351",
        operation: traceCallback,
      });
      const diagnosticResult = button(card(h), "Make the command")!.click();
      traceActiveOperation = traceCallback;
      traceSpeech("act.callback.return", {
        callback: "act@fa3:351",
        operation: traceCallback,
      });
      return diagnosticResult;
    });
    traceEnd(traceAwait29);
    const traceAwait30 = traceBegin("flushAll@fa3:352");
    await flushAll();
    traceEnd(traceAwait30);
    expect(h.calls.preview).toHaveLength(1);
    const traceAwait31 = traceBegin("speak@fa3:354");
    await speak(h);
    traceEnd(traceAwait31);
    const traceAwait32 = traceBegin("advance@fa3:355");
    await advance(800);
    traceEnd(traceAwait32);
    const traceAwait33 = traceBegin("act@fa3:356");
    await act(async () => {
      const traceCallback = traceActiveOperation;
      traceSpeech("act.callback.enter", {
        callback: "act@fa3:356",
        operation: traceCallback,
      });
      const diagnosticResult = use(h)!.click();
      traceActiveOperation = traceCallback;
      traceSpeech("act.callback.return", {
        callback: "act@fa3:356",
        operation: traceCallback,
      });
      return diagnosticResult;
    });
    traceEnd(traceAwait33);
    const result = h.host.querySelector(
      "[data-testid=marvel0-command-result]",
    )!;
    expect(result.getAttribute("data-stale")).toBe("true");
    expect(
      result.querySelector("[data-testid=marvel0-command-text]"),
    ).toBeNull();
    const traceAwait34 = traceBegin("advance@fa3:364");
    await advance(1000);
    traceEnd(traceAwait34);
    expect(h.calls.preview).toHaveLength(1);
    expect(h.calls.preview).toHaveLength(1);
  });

  test("Enter in the transcript's editor matches again and does not press 'Make the command'", async () => {
    const traceAwait35 = traceBegin("open@fa3:370");
    const h = await open(() => matchC());
    traceEnd(traceAwait35);
    const traceAwait36 = traceBegin("speak@fa3:371");
    await speak(h);
    traceEnd(traceAwait36);
    const traceAwait37 = traceBegin("act@fa3:372");
    await act(async () => {
      const traceCallback = traceActiveOperation;
      traceSpeech("act.callback.enter", {
        callback: "act@fa3:372",
        operation: traceCallback,
      });
      const diagnosticResult = button(h.host, "Edit the text")!.click();
      traceActiveOperation = traceCallback;
      traceSpeech("act.callback.return", {
        callback: "act@fa3:372",
        operation: traceCallback,
      });
      return diagnosticResult;
    });
    traceEnd(traceAwait37);
    const input = h.host.querySelector<HTMLInputElement>(
      "input[data-speech-draft]",
    )!;
    const traceAwait38 = traceBegin("type@fa3:376");
    await type(input, "stack plates");
    traceEnd(traceAwait38);
    const enter = new KeyboardEvent("keydown", {
      key: "Enter",
      bubbles: true,
      cancelable: true,
    });
    const traceAwait39 = traceBegin("fire@fa3:382");
    await fire(input, enter);
    traceEnd(traceAwait39);
    const traceAwait40 = traceBegin("flushAll@fa3:383");
    await flushAll();
    traceEnd(traceAwait40);
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
    const traceAwait41 = traceBegin("open@fa3:398");
    const h = await open(() =>
      restricted
        ? matchC({ family: "restricted", allowed: RESTRICTED })
        : matchA(KNOWN[2]),
    );
    traceEnd(traceAwait41);
    const traceAwait42 = traceBegin("speak@fa3:403");
    await speak(h);
    traceEnd(traceAwait42);
    expect(
      h.host.querySelector("[data-testid=marvel0-speech-best]"),
    ).not.toBeNull();
    restricted = true;
    const traceAwait43 = traceBegin("h.select@fa3:408");
    await h.select(SP);
    traceEnd(traceAwait43);
    const traceAwait44 = traceBegin("flushAll@fa3:409");
    await flushAll();
    traceEnd(traceAwait44);
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
    const traceAwait45 = traceBegin("advance@fa3:422");
    await advance(800);
    traceEnd(traceAwait45);
    const traceAwait46 = traceBegin("act@fa3:423");
    await act(async () => {
      const traceCallback = traceActiveOperation;
      traceSpeech("act.callback.enter", {
        callback: "act@fa3:423",
        operation: traceCallback,
      });
      const diagnosticResult = radios[0].click();
      traceActiveOperation = traceCallback;
      traceSpeech("act.callback.return", {
        callback: "act@fa3:423",
        operation: traceCallback,
      });
      return diagnosticResult;
    });
    traceEnd(traceAwait46);
    const traceAwait47 = traceBegin("act@fa3:424");
    await act(async () => {
      const traceCallback = traceActiveOperation;
      traceSpeech("act.callback.enter", {
        callback: "act@fa3:424",
        operation: traceCallback,
      });
      const diagnosticResult = use(h)!.click();
      traceActiveOperation = traceCallback;
      traceSpeech("act.callback.return", {
        callback: "act@fa3:424",
        operation: traceCallback,
      });
      return diagnosticResult;
    });
    traceEnd(traceAwait47);
    expect(prompt(h).value).toBe(RESTRICTED[0]);
  });
});

describe("the action section of the page", () => {
  test("has one microphone control, outside every dialog, even with the policy server card beside it", async () => {
    audio = installFakeAudio();
    network();
    const { api } = fakeApi();
    const traceAwait48 = traceBegin("render@fa3:434");
    const view = await render(<Marvel0Actions api={api} />);
    traceEnd(traceAwait48);
    const traceAwait49 = traceBegin("flushAll@fa3:435");
    await flushAll();
    traceEnd(traceAwait49);
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
