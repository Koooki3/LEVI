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
import { fakeApi, button, type as typeInto } from "./actions-helpers";
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
  test("waits for exit, recovers an unknown on a later same-run poll, and resets for a new run", async () => {
    const { api } = fakeApi();
    const calls: string[] = [];
    let throwOnPress = false;
    const actions: RunActions = {
      working: null,
      holding: false,
      refused: null,
      press: async (action) => {
        calls.push(action);
        if (throwOnPress) throw new Error("connection interrupted");
        return true;
      },
      hold: async () => true,
      end: async () => true,
      start: async () => true,
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
    const view = await render(page("at_gate", "run-1"));
    await waitFor(
      () => view.host.querySelector("[data-testid=marvel0-command]"),
      { label: "the command form" },
    );
    await waitFor(
      () =>
        view.host.querySelector("[data-testid=marvel0-episode-change-open]"),
      { label: "the gate action" },
    );

    await click(button(view.host, en["marvel0.episodeChange.open"])!);
    const firstPrompt = view.host.querySelector(
      "[data-testid=marvel0-episode-change-prompt]",
    ) as HTMLInputElement;
    await typeInto(firstPrompt, "next task");
    await click(button(view.host, en["marvel0.episodeChange.endAndPrepare"])!);
    await waitFor(
      () =>
        view.host.querySelector(
          '[data-testid="marvel0-episode-change-status"][data-state="waiting"]',
        ),
      { label: "waiting for exit after the HTTP acknowledgement" },
    );
    expect(calls).toEqual(["end_at_gate"]);
    expect(
      view.host.querySelector("[data-testid=marvel0-command-resumed]"),
    ).toBeNull();

    await view.rerender(page("done", "run-1"));
    await waitFor(
      () =>
        view.host.querySelector(
          '[data-testid="marvel0-episode-change-status"][data-state="ready"]',
        ),
      { label: "same-run exit and lock-release proof" },
    );
    await waitFor(
      () => view.host.querySelector("[data-testid=marvel0-command-resumed]"),
      { label: "the preserved form" },
    );
    let prompt = view.host.querySelector(
      '[data-testid="marvel0-command"] input[list="marvel0-known-prompts"]',
    ) as HTMLInputElement;
    expect(prompt.value).toBe("next task");
    expect(
      (
        view.host.querySelector(
          '[data-testid="marvel0-command"] input',
        ) as HTMLInputElement
      ).disabled,
    ).toBe(false);

    // A new run clears the old operation and unpins the command form while
    // carrying the changed instruction into ordinary editable fields.
    await view.rerender(page("at_gate", "run-2"));
    await waitFor(
      () =>
        !view.host.querySelector("[data-testid=marvel0-command-resumed]") &&
        view.host.querySelector("[data-testid=marvel0-episode-change-open]"),
      { label: "the next run's fresh workflow" },
    );
    prompt = view.host.querySelector(
      '[data-testid="marvel0-command"] input[list="marvel0-known-prompts"]',
    ) as HTMLInputElement;
    expect(prompt.value).toBe("next task");

    throwOnPress = true;
    await click(button(view.host, en["marvel0.episodeChange.open"])!);
    const nextPrompt = view.host.querySelector(
      "[data-testid=marvel0-episode-change-prompt]",
    ) as HTMLInputElement;
    await typeInto(nextPrompt, "third task");
    await click(button(view.host, en["marvel0.episodeChange.endAndPrepare"])!);
    await waitFor(
      () =>
        view.host.querySelector(
          '[data-testid="marvel0-episode-change-status"][data-state="unknown"]',
        ),
      { label: "the uncertain request" },
    );
    expect(calls).toEqual(["end_at_gate", "end_at_gate"]);

    await act(async () => view.rerender(page("done", "run-2")));
    await waitFor(
      () =>
        view.host.querySelector(
          '[data-testid="marvel0-episode-change-status"][data-state="ready"]',
        ),
      { label: "later confirmation without another mutation" },
    );
    expect(calls).toEqual(["end_at_gate", "end_at_gate"]);
    prompt = view.host.querySelector(
      '[data-testid="marvel0-command"] input[list="marvel0-known-prompts"]',
    ) as HTMLInputElement;
    expect(prompt.value).toBe("third task");
  });
});
