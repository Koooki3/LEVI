// The hook behind the voice card, driven without its buttons: what it hands to
// onUse no matter how it is called.
import { render, setupDom } from "@/components/ds/__tests__/dom";
import { act } from "react";
import { afterEach, describe, expect, jest, test } from "bun:test";
import { useSpeechSession, type SpeechSession } from "../speech-session";
import {
  KNOWN,
  captureFactory,
  fakeSpeechApi,
  heardAnswer,
  installFakeAudio,
  matchA,
  matchV,
  ok,
  type FakeAudio,
} from "./speech-fixtures";

setupDom();

let audio: FakeAudio | null = null;
afterEach(() => {
  audio?.restore();
  audio = null;
  jest.useRealTimers();
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

/** Calls use() the way a click would: inside act. */
const press = (h: { session: () => SpeechSession }, ...prompts: string[]) =>
  act(async () => {
    for (const prompt of prompts) h.session().use(prompt);
  });

async function open(match: Record<string, unknown> = matchA(KNOWN[2])) {
  jest.useFakeTimers();
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
