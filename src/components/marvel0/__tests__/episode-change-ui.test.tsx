import { act } from "react";
import { afterEach, describe, expect, test } from "bun:test";
import en from "@/i18n/en.json";
import {
  click,
  render,
  setupDom,
  waitFor,
} from "@/components/ds/__tests__/dom";
import { Marvel0Actions } from "../marvel0-actions";
import { runView } from "../run-logic";
import type { RunActions } from "../use-run-actions";
import type { RunPoll } from "../use-run-poll";
import { fakeApi, button, tickAll, type as typeInto } from "./actions-helpers";
import { runDoc } from "./ctl-fixtures";

setupDom();
afterEach(() => {
  try {
    localStorage.clear();
  } catch {
    // no storage in this DOM
  }
});

const known = new Set(Object.keys(en));
const savedForm = {
  model_id: "pi05_fr3_all_step49999",
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

function poll(state: string, runId: string): RunPoll {
  const doc = runDoc(state, (entry) => {
    entry.run = {
      run_id: runId,
      phase: state === "done" ? "ended" : "started",
      lock_released: state === "done",
      form: savedForm,
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

describe("the next-instruction workflow on the full action page", () => {
  type Started = {
    form: Record<string, unknown>;
    phrase: string;
    ack: Record<string, boolean>;
  };
  function harness(startResult: () => Promise<boolean>) {
    const { api } = fakeApi();
    const calls: string[] = [];
    const starts: Started[] = [];
    const control = { throwOnPress: false };
    const actions: RunActions = {
      working: null,
      holding: false,
      refused: null,
      press: async (action) => {
        calls.push(action);
        if (control.throwOnPress) throw new Error("connection interrupted");
        return true;
      },
      hold: async () => true,
      end: async () => true,
      start: async (form, phrase, ack) => {
        calls.push("start");
        starts.push({ form, phrase, ack: { ...ack } });
        return startResult();
      },
      clear: () => {},
    };
    const page = (state: string, runId: string) => (
      <Marvel0Actions
        api={api}
        pollMs={10_000}
        run={poll(state, runId)}
        runActions={actions}
      />
    );
    return { calls, starts, control, page };
  }

  async function changeTo(host: HTMLElement, prompt: string) {
    const field = (await waitFor(
      () => host.querySelector("[data-testid=marvel0-episode-change-prompt]"),
      { label: "the gate's instruction field" },
    )) as HTMLInputElement;
    await typeInto(field, prompt);
    await click(button(host, en["marvel0.episodeChange.restart"])!);
    const win = host.ownerDocument.querySelector(
      "[role=dialog]",
    ) as HTMLElement;
    await tickAll(win);
    await click(button(win, en["marvel0.episodeChange.go"])!);
  }

  const status = (host: HTMLElement, state: string) =>
    host.querySelector(
      `[data-testid="marvel0-episode-change-status"][data-state="${state}"]`,
    );

  test("ends at the gate, waits for the same run's exit, then starts the new client once with the saved form, the new instruction and the ticks", async () => {
    const h = harness(async () => true);
    const view = await render(h.page("at_gate", "run-1"));
    await changeTo(view.host, "next task");
    await waitFor(() => status(view.host, "waiting"), {
      label: "waiting for exit after the HTTP acknowledgement",
    });
    expect(h.calls).toEqual(["end_at_gate"]);

    // Still the old client, still at the gate: nothing more is sent.
    await view.rerender(h.page("at_gate", "run-1"));
    expect(h.calls).toEqual(["end_at_gate"]);

    await view.rerender(h.page("done", "run-1"));
    await waitFor(() => status(view.host, "restarting"), {
      label: "the automatic start",
    });
    expect(h.calls).toEqual(["end_at_gate", "start"]);
    expect(h.starts[0].form).toEqual({ ...savedForm, prompt: "next task" });
    expect(h.starts[0].phrase).toBe("start client saved_group");
    expect(h.starts[0].ack).toEqual({
      estop: true,
      scene_reset: true,
      area_clear: true,
    });
    // Further reads of the ended run start nothing more.
    await view.rerender(h.page("done", "run-1"));
    expect(h.calls).toEqual(["end_at_gate", "start"]);
    expect(
      view.host.querySelector("[data-testid=marvel0-command-resumed]"),
    ).toBeNull();

    // The new client reaches its gate: a fresh, empty field.
    await view.rerender(h.page("at_gate", "run-2"));
    const field = (await waitFor(
      () =>
        view.host.querySelector("[data-testid=marvel0-episode-change-prompt]"),
      { label: "the next run's field" },
    )) as HTMLInputElement;
    expect(field.value).toBe("");
  });

  test("a refused start fills step 3 with the saved form and the new instruction instead", async () => {
    const h = harness(async () => false);
    const view = await render(h.page("at_gate", "run-1"));
    await changeTo(view.host, "next task");
    await view.rerender(h.page("done", "run-1"));
    await waitFor(() => status(view.host, "ready"), {
      label: "the manual fallback",
    });
    expect(h.calls).toEqual(["end_at_gate", "start"]);
    await waitFor(
      () => view.host.querySelector("[data-testid=marvel0-command-resumed]"),
      { label: "the preserved form" },
    );
    const prompt = view.host.querySelector(
      '[data-testid="marvel0-command"] input[list="marvel0-known-prompts"]',
    ) as HTMLInputElement;
    expect(prompt.value).toBe("next task");
  });

  test("an interrupted end is never resent; a later confirmed exit of the same run still restarts once", async () => {
    const h = harness(async () => true);
    h.control.throwOnPress = true;
    const view = await render(h.page("at_gate", "run-1"));
    await changeTo(view.host, "third task");
    await waitFor(() => status(view.host, "unknown"), {
      label: "the uncertain request",
    });
    expect(h.calls).toEqual(["end_at_gate"]);
    await act(async () => view.rerender(h.page("done", "run-1")));
    await waitFor(() => status(view.host, "restarting"), {
      label: "later confirmation without another end",
    });
    expect(h.calls).toEqual(["end_at_gate", "start"]);
    expect(h.starts[0].form.prompt).toBe("third task");
  });
});
