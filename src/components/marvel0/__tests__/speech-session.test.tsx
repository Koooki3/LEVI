// The hook behind the voice card, driven without its buttons: what it hands to
// onUse no matter how it is called.
import { render, setupDom } from "@/components/ds/__tests__/dom";
import { act } from "react";
import { advanceInSteps, installApplicationClock } from "./application-clock";
import {
  afterAll,
  afterEach,
  beforeAll,
  describe,
  expect,
  test,
} from "bun:test";
import { useSpeechSession, type SpeechSession } from "../speech-session";
import {
  KNOWN,
  TENNIS,
  asHeard,
  captureFactory,
  fakeSpeechApi,
  heardAnswer,
  installFakeAudio,
  matchA,
  matchC,
  matchV,
  ok,
  type FakeAudio,
} from "./speech-fixtures";

// Install after React loads so its captured host clocks stay real. The
// application clock stays through cleanup; restore it before the DOM globals go.
let clock: ReturnType<typeof installApplicationClock>;
beforeAll(() => {
  clock = installApplicationClock();
});
afterAll(() => clock.restore());
setupDom();

let audio: FakeAudio | null = null;
afterEach(() => {
  audio?.restore();
  audio = null;
});

async function flushAll() {
  await act(async () => {
    for (let i = 0; i < 30; i += 1) await Promise.resolve();
  });
}
const advance = (ms: number) =>
  act(async () => {
    await advanceInSteps(clock, ms, async () => {
      for (let i = 0; i < 10; i += 1) await Promise.resolve();
    });
  });

/** Calls use() the way a click would: inside act. */
const press = (h: { session: () => SpeechSession }, ...prompts: string[]) =>
  act(async () => {
    for (const prompt of prompts) h.session().use(prompt);
  });

async function open(match: Record<string, unknown> = matchA(KNOWN[2])) {
  audio = installFakeAudio();
  const captures = captureFactory();
  const { api } = fakeSpeechApi({
    transcribe: (call) => ok(heardAnswer(call.requestId, match)),
  });
  const used: string[] = [];
  const latest: { current: SpeechSession | null } = { current: null };
  function Probe() {
    const session = useSpeechSession({
      api,
      modelId: "m",
      onUse: (prompt) => used.push(prompt),
      createCapture: captures.make,
    });
    latest.current = session;
    return <button ref={session.attach} data-testid="mic" />;
  }
  const view = await render(<Probe />);
  await flushAll();
  const mic = view.host.querySelector("button")!;
  await act(async () => {
    mic.dispatchEvent(
      new PointerEvent("pointerdown", {
        pointerId: 1,
        pointerType: "mouse",
        button: 0,
        bubbles: true,
        cancelable: true,
      }),
    );
  });
  await act(async () => captures.current.deliver());
  await flushAll();
  return { ...view, used, session: () => latest.current! };
}

describe("use()", () => {
  test("hands over nothing before the proposal has been on screen for 800 ms", async () => {
    const h = await open();
    expect(h.session().state.proposal).not.toBeNull();
    await press(h, KNOWN[2]);
    await advance(799);
    await press(h, KNOWN[2]);
    expect(h.used).toEqual([]);
    await advance(1);
    await press(h, KNOWN[2]);
    expect(h.used).toEqual([KNOWN[2]]);
  });

  test("hands over a sentence once, even when called twice in one tick", async () => {
    const h = await open();
    await advance(800);
    await press(h, KNOWN[2], KNOWN[2]);
    expect(h.used).toEqual([KNOWN[2]]);
  });

  test("hands over only what is on offer: not another allowed sentence, not text of its own", async () => {
    const h = await open();
    await advance(800);
    await press(h, KNOWN[0]); // allowed, but tier A offers one sentence
    await press(h, "pick the eggplant in the blue plate; rm -rf /");
    await press(h, "");
    expect(h.used).toEqual([]);
  });

  test("hands over nothing from a result with a flag, however it is called", async () => {
    const h = await open(matchV("stop"));
    await advance(800);
    await press(h, ...KNOWN);
    expect(h.used).toEqual([]);
  });

  test("hands over nothing once the proposal has expired", async () => {
    const h = await open();
    await advance(800);
    await advance(60_000 - 800);
    expect(h.session().state.proposal).toBeNull();
    await press(h, KNOWN[2]);
    expect(h.used).toEqual([]);
  });
});

describe("use() of the sentence as heard", () => {
  test("hands it over once the proposal is armed, and only exactly as the server tidied it", async () => {
    const h = await open(matchC({ as_heard: asHeard(TENNIS) }));
    await press(h, TENNIS);
    expect(h.used).toEqual([]); // not before the 800 ms
    await advance(800);
    await press(h, TENNIS.toUpperCase()); // not a spelling of its own
    await press(h, `${TENNIS}.`);
    await press(h, "pick the cup");
    expect(h.used).toEqual([]);
    await press(h, TENNIS, TENNIS);
    expect(h.used).toEqual([TENNIS]);
  });

  const refused: Array<[string, Record<string, unknown>]> = [
    [
      "a sentence the task instruction field would refuse",
      matchC({ as_heard: asHeard("pick the cup's lid", "forbidden_char") }),
    ],
    [
      "a restricted model",
      matchC({ family: "restricted", as_heard: asHeard(TENNIS) }),
    ],
    ["a flagged result", matchV("stop", { as_heard: asHeard(TENNIS) })],
    ["an answer without one", matchC()],
  ];
  for (const [name, raw] of refused)
    test(`hands over nothing for ${name}`, async () => {
      const h = await open(raw);
      await advance(800);
      await press(h, TENNIS, "pick the cup's lid");
      expect(h.used).toEqual([]);
    });
});

describe("the timing of an answer", () => {
  test("is the page's wait from the release of the button, with the server's numbers", async () => {
    audio = installFakeAudio();
    const captures = captureFactory();
    const { api } = fakeSpeechApi({
      transcribe: (call) => {
        clock.advance(1800);
        return ok(
          heardAnswer(call.requestId, matchA(KNOWN[2]), {
            timing_ms: { load: 0, infer: 900, match: 100, total: 1200 },
          }),
        );
      },
    });
    const latest: { current: SpeechSession | null } = { current: null };
    function Probe() {
      const session = useSpeechSession({
        api,
        modelId: "m",
        onUse: () => undefined,
        createCapture: captures.make,
      });
      latest.current = session;
      return <button ref={session.attach} />;
    }
    const view = await render(<Probe />);
    await flushAll();
    await act(async () => {
      view.host.querySelector("button")!.dispatchEvent(
        new PointerEvent("pointerdown", {
          pointerId: 1,
          pointerType: "mouse",
          button: 0,
          bubbles: true,
          cancelable: true,
        }),
      );
    });
    expect(latest.current!.state.timing).toBeNull();
    await advance(5_000); // held for five seconds: not part of the wait
    await act(async () => captures.current.deliver());
    await flushAll();
    expect(latest.current!.state.timing).toEqual({
      totalMs: 1800,
      server: { load: 0, infer: 900, match: 100, total: 1200 },
    });
  });
});
