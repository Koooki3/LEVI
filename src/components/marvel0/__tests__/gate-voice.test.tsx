// The Run card's voice input at the client's start gate: a spoken instruction
// fills the "Next episode instruction" field and does nothing else. The person
// still presses "Change instruction and restart the client" and ticks the
// confirm window; the voice never changes, starts or enters anything.
import { render, setupDom } from "@/components/ds/__tests__/dom";
import { act } from "react";
import {
  afterAll,
  afterEach,
  beforeAll,
  describe,
  expect,
  test,
} from "bun:test";
import en from "@/i18n/en.json";
import { runView } from "../run-logic";
import {
  RunPanel,
  type ChangeAttest,
  type EpisodeChangeStatus,
} from "../run-panel";
import type { RunActions } from "../use-run-actions";
import type { RunPoll } from "../use-run-poll";
import {
  installApplicationClock,
  installDomApplicationClock,
} from "./application-clock";
import { runDoc } from "./ctl-fixtures";
import {
  KNOWN,
  captureFactory,
  fakeSpeechApi,
  heardAnswer,
  installFakeAudio,
  matchA,
  ok,
  statusAnswer,
  type FakeAudio,
  type Script,
} from "./speech-fixtures";

// Install after React loads so its captured host clocks stay real.
let clock: ReturnType<typeof installApplicationClock>;
beforeAll(() => {
  clock = installDomApplicationClock();
});
afterAll(() => clock.restore());
setupDom();

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

const known = new Set(Object.keys(en));
const MODEL = "pi05_fr3_all_step49999";
const savedForm = {
  model_id: MODEL,
  prompt: "old task",
  max_steps: 500,
  eval_num: 5,
  min_z: 0.025,
  rtc: true,
  rtc_horizon: 10,
  rollout_group: "saved_group",
  note: "saved",
  control_hz: 10,
};

function poll(
  state: string,
  form: Record<string, unknown> = savedForm,
): RunPoll {
  const doc = runDoc(state, (entry) => {
    entry.run = {
      run_id: "run-1",
      phase: state === "done" ? "ended" : "started",
      lock_released: state === "done",
      form,
    };
  });
  const value = runView(doc, known)!;
  return {
    loaded: true,
    value,
    current: true,
    problem: null,
    lastOk: Date.now(),
    refresh: async () => {},
    lastSeen: { state: value.state, mayMove: value.mayMove },
  };
}

async function flushAll() {
  await act(async () => {
    for (let i = 0; i < 30; i += 1) await Promise.resolve();
  });
}
const advance = (ms: number) =>
  act(async () => {
    clock.advance(ms);
    for (let i = 0; i < 10; i += 1) await Promise.resolve();
  });

async function open(
  state: string,
  {
    script = {},
    form = savedForm,
    episodeChange = { state: "idle" },
  }: {
    script?: Script;
    form?: Record<string, unknown>;
    episodeChange?: EpisodeChangeStatus;
  } = {},
) {
  audio = installFakeAudio({ secure: true });
  const captures = captureFactory();
  const speech = fakeSpeechApi({
    transcribe: (call) => ok(heardAnswer(call.requestId, matchA(KNOWN[2]))),
    ...script,
  });
  // Everything the Run card can send; none of it is ever expected here.
  const sent: string[] = [];
  const changes: Array<{ prompt: string; attest: ChangeAttest }> = [];
  const actions: RunActions = {
    working: null,
    holding: false,
    refused: null,
    press: async (action) => {
      sent.push(`press:${action}`);
      return true;
    },
    hold: async () => {
      sent.push("hold");
      return true;
    },
    end: async () => {
      sent.push("end");
      return true;
    },
    start: async () => {
      sent.push("start");
      return true;
    },
    clear: () => {},
  };
  const page = (
    current: string,
    status: EpisodeChangeStatus = episodeChange,
    saved: Record<string, unknown> = form,
  ) => (
    <RunPanel
      poll={poll(current, saved)}
      actions={actions}
      episodeChange={status}
      onChangeInstruction={(prompt, attest) => changes.push({ prompt, attest })}
      speech={{ api: speech.api, createCapture: captures.make }}
    />
  );
  const view = await render(page(state));
  await flushAll();
  return {
    ...view,
    ...speech,
    captures,
    sent,
    changes,
    show: async (
      current: string,
      status: EpisodeChangeStatus = episodeChange,
      saved: Record<string, unknown> = form,
    ) => {
      await view.rerender(page(current, status, saved));
      await flushAll();
    },
  };
}
type H = Awaited<ReturnType<typeof open>>;

const q = (h: H, testid: string) =>
  h.host.querySelector<HTMLElement>(`[data-testid=${testid}]`);
const card = (h: H) => q(h, "marvel0-speech");
const mic = (h: H) => q(h, "marvel0-speech-mic") as HTMLButtonElement;
const field = (h: H) =>
  q(h, "marvel0-episode-change-prompt") as HTMLInputElement | null;
const dialog = (h: H) => h.host.ownerDocument.querySelector("[role=dialog]");

function fire(target: Element, event: Event) {
  return act(async () => {
    target.dispatchEvent(event);
  });
}
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

/** Holds the talk button, lets the clip finish and the answer come back. */
async function speak(h: H) {
  await pointer(mic(h), "pointerdown");
  await advance(400);
  await pointer(mic(h), "pointerup");
  await act(async () => h.captures.current.deliver());
  await flushAll();
}

describe("voice input at the start gate", () => {
  test("the voice card sits with the next-instruction field, reads the voice state once, and has no voice-run switch", async () => {
    const h = await open("at_gate");
    expect(card(h)).not.toBeNull();
    expect(
      q(h, "marvel0-episode-change")!.contains(card(h)),
      "inside the gate's section",
    ).toBe(true);
    expect(h.calls.status).toBe(1);
    expect(mic(h).disabled).toBe(false);
    expect(q(h, "marvel0-episode-change-voice-hint")?.textContent).toBe(
      en["marvel0.episodeChange.voiceHint"],
    );
    // fill-only wording, no "voice run" switch to turn on
    expect(card(h)!.textContent).toContain(en["marvel0.speech.scope_fill"]);
    expect(q(h, "marvel0-speech-run")).toBeNull();
    expect(field(h)).not.toBeNull();
  });

  test("a spoken sentence only fills the field: nothing is changed, started, entered, ended or confirmed", async () => {
    const h = await open("at_gate");
    expect(field(h)!.value).toBe("");
    await speak(h);
    // asked for the running client's model
    expect(h.calls.transcribe).toHaveLength(1);
    expect(h.calls.transcribe[0].modelId).toBe(MODEL);
    // the proposal is armed after 800 ms, then a person presses Use
    await advance(800);
    const use = q(h, "marvel0-speech-use") as HTMLButtonElement;
    expect(use).not.toBeNull();
    // before Use, the field is untouched
    expect(field(h)!.value).toBe("");
    await act(async () => use.click());
    await flushAll();
    expect(field(h)!.value).toBe(KNOWN[2]);
    // ... and that is all
    expect(h.sent).toEqual([]);
    expect(h.changes).toEqual([]);
    expect(dialog(h)).toBeNull();
    // The change button is there for the person; its window is not open.
    expect(
      q(h, "marvel0-episode-change-open") as HTMLButtonElement | null,
    ).not.toBeNull();
    expect(h.host.querySelector("input[type=checkbox]:checked")).toBeNull();
    await advance(5000);
    expect(h.sent).toEqual([]);
    expect(h.changes).toEqual([]);
  });

  test("the sentence is the person's to confirm: the window opens only from the change button and shows it", async () => {
    const h = await open("at_gate");
    await speak(h);
    await advance(800);
    await act(async () =>
      (q(h, "marvel0-speech-use") as HTMLButtonElement).click(),
    );
    await flushAll();
    await act(async () =>
      (q(h, "marvel0-episode-change-open") as HTMLButtonElement).click(),
    );
    await flushAll();
    expect(dialog(h)).not.toBeNull();
    expect(
      h.host.ownerDocument.querySelector(
        "[data-testid=marvel0-episode-change-to]",
      )?.textContent,
    ).toBe(KNOWN[2]);
    // opening it sent nothing yet
    expect(h.sent).toEqual([]);
    expect(h.changes).toEqual([]);
  });

  test.each([
    "starting",
    "homing",
    "running",
    "awaiting_label",
    "finishing_episode",
    "held",
    "lost",
    "done",
    "failed",
    "idle_ready",
  ])("no voice card when the run is not at its gate: %s", async (state) => {
    const h = await open(state);
    expect(card(h)).toBeNull();
    expect(q(h, "marvel0-speech-mic")).toBeNull();
    expect(q(h, "marvel0-episode-change-voice-hint")).toBeNull();
    expect(h.calls.status).toBe(0);
  });

  test("the card appears with the gate and reads the voice state then, not 30 s later; it goes with the gate and frees the speech model", async () => {
    const h = await open("running");
    expect(card(h)).toBeNull();
    expect(h.calls.status).toBe(0);
    await h.show("at_gate");
    expect(card(h)).not.toBeNull();
    expect(h.calls.status).toBe(1);
    expect(mic(h).disabled).toBe(false);
    expect(h.calls.release).toBe(0);
    // the next episode starts
    await h.show("running");
    expect(card(h)).toBeNull();
    expect(h.calls.release).toBe(1);
    // and a later gate opens a fresh card
    await h.show("at_gate");
    expect(card(h)).not.toBeNull();
    expect(h.calls.status).toBe(2);
  });

  test("a gate that is still closed for speech shows the card's own reason, with the mic off", async () => {
    const h = await open("at_gate", {
      script: {
        status: ok(
          statusAnswer({
            gate: { allowed: false, reasons: ["policy_starting"] },
          }),
        ),
      },
    });
    expect(mic(h).disabled).toBe(true);
    expect(q(h, "marvel0-speech-reasons")!.textContent).toContain(
      en["marvel0.speech.gate.policy_starting"],
    );
    // the field still takes typing
    expect(field(h)).not.toBeNull();
  });

  test("the change in flight takes the card away and frees the model", async () => {
    const h = await open("at_gate");
    expect(card(h)).not.toBeNull();
    await h.show("at_gate", { state: "ending", runId: "run-1" });
    expect(card(h)).toBeNull();
    expect(field(h)).toBeNull();
    expect(h.calls.release).toBe(1);
  });

  test("a saved run without a model has no voice card (nothing to check a sentence against), and the field still types", async () => {
    const withoutModel = { ...savedForm, model_id: null };
    const h = await open("at_gate", { form: withoutModel });
    expect(card(h)).toBeNull();
    expect(field(h)).not.toBeNull();
    expect(h.calls.status).toBe(0);
  });
});
