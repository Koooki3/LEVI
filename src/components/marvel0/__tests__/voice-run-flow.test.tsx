// The voice run on the whole action page, with the real capture over a fake
// browser audio stack, a fake network for the voice routes, a fake Marvel0Api
// and a fake hosted client: from the switch and a press of the microphone to
// the command, the start and the one Enter at the new client's start gate,
// and what must NOT happen on the way.
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
import { Marvel0Actions } from "../marvel0-actions";
import { runView } from "../run-logic";
import type { RunActions } from "../use-run-actions";
import type { RunPoll } from "../use-run-poll";
import { installApplicationClock } from "./application-clock";
import { button, fakeApi, field, ok, tickAll } from "./actions-helpers";
import { preview } from "./command-fixtures";
import { runDoc } from "./ctl-fixtures";
import {
  KNOWN,
  heardAnswer,
  installFakeAudio,
  matchA,
  matchB,
  matchedAnswer,
  statusAnswer,
  type FakeAudio,
} from "./speech-fixtures";

let clock: ReturnType<typeof installApplicationClock>;
beforeAll(() => {
  clock = installApplicationClock();
});
afterAll(() => clock.restore());
setupDom({ unmount: "sync" });

let audio: FakeAudio | null = null;
let restoreFetch = () => {};
afterEach(() => {
  try {
    audio?.restore();
  } finally {
    audio = null;
    restoreFetch();
    restoreFetch = () => {};
  }
});

const known = new Set(Object.keys(en));
const OLD = "20261019T073000-4f2a91";
const NEXT = "20261101T150000-aaaaaa";
const formWith = (prompt: string) => ({
  model_id: "pi05_fr3_all_step49999",
  prompt,
  max_steps: 500,
  eval_num: 5,
  min_z: 0.025,
  rtc: true,
  rtc_horizon: 10,
  rollout_group: "saved_group",
  note: "",
  control_hz: 10,
});

function poll(
  state: string,
  runId: string | null = null,
  prompt = "old task",
): RunPoll {
  const doc = runDoc(state, (entry) => {
    entry.run = runId
      ? {
          run_id: runId,
          phase: state === "done" ? "ended" : "started",
          lock_released: state === "done",
          form: formWith(prompt),
        }
      : null;
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
    for (let i = 0; i < 40; i += 1) await Promise.resolve();
  });
}
const advance = (ms: number) =>
  act(async () => {
    clock.advance(ms);
    for (let i = 0; i < 10; i += 1) await Promise.resolve();
  });

function network(heard: () => Record<string, unknown>) {
  const original = globalThis.fetch;
  globalThis.fetch = (async (input: unknown) => {
    const path = String(input);
    const json = (body: unknown) =>
      new Response(JSON.stringify(body), { status: 200 });
    if (path.split("?")[0] === "/api/levi/marvel0/speech")
      return json(statusAnswer());
    if (path.startsWith("/api/levi/marvel0/speech/transcribe")) {
      const id = new URL(path, "http://127.0.0.1").searchParams.get(
        "request_id",
      )!;
      return json(heardAnswer(id, heard(), { transcript: "叠盘子" }));
    }
    if (path === "/api/levi/marvel0/speech/match")
      return json(matchedAnswer(heard()));
    return new Response("{}", { status: 404 });
  }) as typeof fetch;
  restoreFetch = () => {
    globalThis.fetch = original;
  };
}

type Started = {
  form: Record<string, unknown>;
  phrase: string;
  ack: Record<string, boolean>;
};

async function open(
  first: RunPoll,
  heard: () => Record<string, unknown> = () => matchA(KNOWN[1]),
) {
  audio = installFakeAudio();
  network(heard);
  const { api, calls } = fakeApi({ preview: ok(preview()) });
  const pressed: string[] = [];
  const starts: Started[] = [];
  const actions: RunActions = {
    working: null,
    holding: false,
    refused: null,
    press: async (action) => {
      pressed.push(action);
      return true;
    },
    hold: async () => true,
    end: async () => true,
    start: async (form, phrase, ack) => {
      starts.push({ form, phrase, ack: { ...ack } });
      return true;
    },
    clear: () => {},
  };
  const page = (run: RunPoll) => (
    <Marvel0Actions api={api} pollMs={60_000} run={run} runActions={actions} />
  );
  const view = await render(page(first));
  await flushAll();
  const show = async (run: RunPoll) => {
    await view.rerender(page(run));
    await flushAll();
  };
  return { ...view, calls, pressed, starts, show, audio: audio! };
}
type H = Awaited<ReturnType<typeof open>>;

const byTest = (h: H, id: string) =>
  h.host.querySelector<HTMLElement>(`[data-testid=${id}]`);
const mic = (h: H) => byTest(h, "marvel0-speech-mic") as HTMLButtonElement;
const pointer = (target: Element, type: string) =>
  act(async () => {
    target.dispatchEvent(
      new PointerEvent(type, {
        pointerId: 1,
        pointerType: "mouse",
        button: 0,
        bubbles: true,
        cancelable: true,
      }),
    );
  });
async function speak(h: H, ms = 1000) {
  await pointer(mic(h), "pointerdown");
  await flushAll();
  await act(async () => h.audio.seconds(ms / 1000));
  await advance(ms);
  await pointer(mic(h), "pointerup");
  await act(async () => h.audio.seconds(0.2));
  await advance(200);
  await flushAll();
}
async function switchOn(h: H) {
  await act(async () => byTest(h, "marvel0-speech-run-on")!.click());
  const dialog = h.host.ownerDocument.querySelector(
    "[role=dialog]",
  ) as HTMLElement;
  expect(
    dialog.querySelector("[data-testid=marvel0-speech-run-dialog]"),
  ).not.toBeNull();
  const go = button(dialog, en["marvel0.speech.run.dialog.go"])!;
  expect(go.disabled).toBe(true);
  await tickAll(dialog);
  expect(go.disabled).toBe(false);
  await act(async () => go.click());
  await flushAll();
}
const status = (h: H) =>
  byTest(h, "marvel0-speech-run-status")?.querySelector("[data-phase]");
const taskField = (h: H) =>
  field(byTest(h, "marvel0-command")!, "Task instruction") as HTMLInputElement;

describe("the voice run on the action page", () => {
  test("off by default: a tier-A answer only proposes, and Use only fills the field", async () => {
    const h = await open(poll("idle_ready"));
    expect(byTest(h, "marvel0-speech-run")?.dataset.on).toBe("false");
    await speak(h);
    await advance(5000);
    expect(byTest(h, "marvel0-speech-run-auto")).toBeNull();
    expect(h.calls.preview).toHaveLength(0);
    expect(h.starts).toHaveLength(0);
    await act(async () => byTest(h, "marvel0-speech-use")!.click());
    await flushAll();
    expect(taskField(h).value).toBe(KNOWN[1]);
    expect(h.calls.preview).toHaveLength(0);
    expect(h.starts).toHaveLength(0);
    expect(h.pressed).toEqual([]);
  });

  test("on: a clear answer makes the command and starts the client after 2 s with the ticks, then presses Enter once at that client's gate", async () => {
    const h = await open(poll("idle_ready"));
    await switchOn(h);
    expect(byTest(h, "marvel0-speech-run-warning")).not.toBeNull();
    await speak(h);
    expect(byTest(h, "marvel0-speech-run-auto")?.textContent).toContain(
      KNOWN[1],
    );
    await advance(1500);
    expect(h.calls.preview).toHaveLength(0);
    expect(h.starts).toHaveLength(0);
    await advance(600);
    await flushAll();
    expect(h.calls.preview).toHaveLength(1);
    expect(h.calls.preview[0].prompt).toBe(KNOWN[1]);
    expect(h.starts).toHaveLength(1);
    expect(h.starts[0].form).toEqual(h.calls.preview[0]);
    expect(h.starts[0].ack).toEqual({
      estop: true,
      scene_reset: true,
      area_clear: true,
    });
    expect(h.starts[0].phrase).toContain(
      String(h.calls.preview[0].rollout_group ?? ""),
    );
    expect(taskField(h).value).toBe(KNOWN[1]);
    expect(status(h)?.getAttribute("data-phase")).toBe("waiting");
    expect(h.pressed).toEqual([]);

    // the client starts, then reaches its gate with the spoken instruction
    await h.show(poll("starting", NEXT, KNOWN[1]));
    expect(h.pressed).toEqual([]);
    await h.show(poll("at_gate", NEXT, KNOWN[1]));
    expect(h.pressed).toEqual(["enter"]);
    expect(status(h)?.getAttribute("data-phase")).toBe("entered");
    await h.show(poll("at_gate", NEXT, KNOWN[1]));
    await advance(5000);
    expect(h.pressed).toEqual(["enter"]);
  });

  test("Cancel stops the run of that answer; nothing is made or started", async () => {
    const h = await open(poll("idle_ready"));
    await switchOn(h);
    await speak(h);
    await act(async () => byTest(h, "marvel0-speech-run-cancel")!.click());
    await advance(5000);
    expect(byTest(h, "marvel0-speech-run-cancelled")).not.toBeNull();
    expect(h.calls.preview).toHaveLength(0);
    expect(h.starts).toHaveLength(0);
  });

  test("an answer with several candidates waits; the person's Run selected press runs it", async () => {
    const h = await open(poll("idle_ready"), () => matchB());
    await switchOn(h);
    await speak(h);
    expect(
      byTest(h, "marvel0-speech-run-no-auto")?.getAttribute("data-why"),
    ).toBe("choose");
    await advance(5000);
    expect(h.calls.preview).toHaveLength(0);
    const radio = h.host.querySelector<HTMLInputElement>(
      `input[type=radio][value="${KNOWN[0]}"]`,
    )!;
    await act(async () => radio.click());
    const run = byTest(h, "marvel0-speech-use") as HTMLButtonElement;
    expect(run.textContent).toBe(en["marvel0.speech.btn.run_selected"]);
    await act(async () => run.click());
    await flushAll();
    expect(h.calls.preview).toHaveLength(1);
    expect(h.calls.preview[0].prompt).toBe(KNOWN[0]);
    expect(h.starts).toHaveLength(1);
  });

  test("at the gate with the same instruction it presses Enter at once, and nothing is started", async () => {
    const h = await open(poll("at_gate", OLD, KNOWN[1]));
    await switchOn(h);
    await speak(h);
    await advance(2100);
    await flushAll();
    expect(h.pressed).toEqual(["enter"]);
    expect(h.calls.preview).toHaveLength(0);
    expect(h.starts).toHaveLength(0);
  });

  test("at the gate with another instruction it ends the client there, starts it again with the new one and presses Enter at the new gate", async () => {
    const h = await open(poll("at_gate", OLD, "old task"));
    await switchOn(h);
    await speak(h);
    await advance(2100);
    await flushAll();
    expect(h.pressed).toEqual(["end_at_gate"]);
    expect(status(h)?.getAttribute("data-phase")).toBe("changing");
    // the old client exits; the page's change starts the new one
    await h.show(poll("done", OLD, "old task"));
    expect(h.starts).toHaveLength(1);
    expect(h.starts[0].form.prompt).toBe(KNOWN[1]);
    expect(h.starts[0].form.rollout_group).toBe("saved_group");
    expect(h.starts[0].ack).toEqual({
      estop: true,
      scene_reset: true,
      area_clear: true,
    });
    expect(h.calls.preview).toHaveLength(0);
    await h.show(poll("at_gate", NEXT, KNOWN[1]));
    expect(h.pressed).toEqual(["end_at_gate", "enter"]);
  });

  test("while an episode runs, speech is refused and nothing is sent", async () => {
    const h = await open(poll("running", OLD, "old task"));
    await switchOn(h);
    await speak(h);
    await advance(2100);
    await flushAll();
    expect(status(h)?.getAttribute("data-phase")).toBe("refused");
    expect(h.pressed).toEqual([]);
    expect(h.starts).toHaveLength(0);
    expect(h.calls.preview).toHaveLength(0);
  });

  test("a frozen client switches the voice run off; switching off brings back plain Use", async () => {
    const h = await open(poll("running", OLD, "old task"));
    await switchOn(h);
    await h.show(poll("held", OLD, "old task"));
    expect(byTest(h, "marvel0-speech-run")?.dataset.on).toBe("false");
    expect(status(h)?.getAttribute("data-phase")).toBe("off_held");

    await h.show(poll("idle_ready"));
    await switchOn(h);
    await act(async () => byTest(h, "marvel0-speech-run-off")!.click());
    await speak(h);
    await advance(5000);
    expect(h.starts).toHaveLength(0);
    expect(
      (byTest(h, "marvel0-speech-use") as HTMLButtonElement).textContent,
    ).toBe(en["marvel0.speech.btn.use"]);
  });

  test("a client that does not reach its gate in time is not entered later", async () => {
    const h = await open(poll("idle_ready"));
    await switchOn(h);
    await speak(h);
    await advance(2100);
    await flushAll();
    expect(h.starts).toHaveLength(1);
    await h.show(poll("starting", NEXT, KNOWN[1]));
    await advance(120_000);
    await flushAll();
    expect(status(h)?.getAttribute("data-phase")).toBe("late");
    await h.show(poll("at_gate", NEXT, KNOWN[1]));
    expect(h.pressed).toEqual([]);
  });
});
