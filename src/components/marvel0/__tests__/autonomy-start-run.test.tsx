import { afterEach, describe, expect, test } from "bun:test";
import { click, setupDom, waitFor } from "@/components/ds/__tests__/dom";
import en from "@/i18n/en.json";
import { tick } from "./actions-helpers";
import {
  $,
  dialog,
  keyButton,
  openStep,
  serve,
  text,
  wait,
} from "./autonomy-helpers";
import { callsOf, keyOf, readyDoc, refusal, runDoc } from "./autonomy-fixtures";
import { requiredAttest } from "../autonomy-logic";

// L2-STEP5-SIMPLIFY-1117: the click on Start is the word for D's start prompt
// too. The page answers `execute <run>` itself once D shows that prompt, with
// the backend's phrase and the codes of what the click stated; any other screen
// ends it. The window of the start of the run is gone.

setupDom();
afterEach(() => {
  try {
    localStorage.clear();
  } catch {
    // no storage in this DOM
  }
});

const E = en as Record<string, string>;
const allMet = () =>
  readyDoc("prepared_ready", (doc) => {
    for (const row of doc.readiness) row.level = "ok";
  });
const start = (host: ParentNode) =>
  $(host, "marvel0-autonomy-start") as HTMLButtonElement;
const sentKeys = (state: { calls: { op: string }[] }) =>
  callsOf(state as never, "key").map((body) => body.action);

async function clickStart(f: Awaited<ReturnType<typeof openStep>>) {
  await click(start(f.host));
  await tick(4, 10);
  expect(callsOf(f.state, "start")).toHaveLength(1);
}

describe("the answer to D's start prompt after the click on Start", () => {
  test("nothing goes while D launches; when D asks its phrase the page answers once, with the backend's phrase and all seven codes", async () => {
    const f = await openStep({ ready: allMet(), run: runDoc("idle") });
    await clickStart(f);
    await serve(f, runDoc("launching"));
    await wait(700);
    expect(sentKeys(f.state)).toEqual([]);
    expect($(f.host, "marvel0-autonomy-macro")).not.toBeNull();
    await serve(f, runDoc("services"));
    await wait(700);
    expect(sentKeys(f.state)).toEqual([]);
    await serve(f, runDoc("await_phrase"));
    await waitFor(() => sentKeys(f.state).length === 1, { label: "execute" });
    const doc = runDoc("await_phrase");
    const sent = callsOf(f.state, "key")[0];
    expect(sent).toMatchObject({
      action: "execute",
      confirm: keyOf(doc, "execute")!.phrase,
      token: keyOf(doc, "execute")!.token,
      attest: requiredAttest("execute"),
    });
    expect(dialog()).toBeNull();
    await serve(f, runDoc("services"));
    await wait(700);
    expect(sentKeys(f.state)).toEqual(["execute"]);
    expect($(f.host, "marvel0-autonomy-macro-ended")).toBeNull();
  });

  test("the key is not a button to press by hand while the page is sending it", async () => {
    const f = await openStep({
      ready: allMet(),
      run: runDoc("idle"),
      limits: { armDelayMs: 600 },
    });
    await clickStart(f);
    await serve(f, runDoc("await_phrase"));
    expect(keyButton(f.host, "execute")).toBeNull();
    await waitFor(() => sentKeys(f.state).length === 1, { label: "execute" });
  });

  test("a start that was refused arms nothing: a D that shows up later is answered by nobody", async () => {
    const f = await openStep({ ready: allMet(), run: runDoc("idle") });
    f.state.answer = (call) =>
      call.op === "start"
        ? refusal(409, "readiness_failed", { first: "gpu_free" })
        : call.op === "challenge"
          ? { ok: true, status: 200, data: { challenge: "tok" } }
          : { ok: true, status: 202, data: {} };
    await click(start(f.host));
    await tick(4, 10);
    await serve(f, runDoc("await_phrase"));
    await wait(900);
    expect(sentKeys(f.state)).toEqual([]);
    expect(keyButton(f.host, "execute")).not.toBeNull(); // the person's own button
  });

  test("another run's D is not the one the click started", async () => {
    const f = await openStep({ ready: allMet(), run: runDoc("idle") });
    await clickStart(f);
    await serve(
      f,
      runDoc("await_phrase", (doc) => (doc.name = "aeri-20990101-ext-r09")),
    );
    await wait(900);
    expect(sentKeys(f.state)).toEqual([]);
  });

  test("the long start after an unfinished policy switch is not answered by the page: the sequence ends and its window is the person's", async () => {
    const f = await openStep({ ready: allMet(), run: runDoc("idle") });
    await clickStart(f);
    await serve(f, runDoc("await_phrase_variant"));
    await waitFor(() => $(f.host, "marvel0-autonomy-macro-ended") !== null, {
      label: "the note",
    });
    expect(text($(f.host, "marvel0-autonomy-macro-ended"))).toBe(
      E["marvel0.autonomy.macro.ended.unexpected_screen"]
        .replace("{sent}", "0")
        .replace("{total}", "1"),
    );
    await wait(600);
    expect(sentKeys(f.state)).toEqual([]);
    expect(keyButton(f.host, "execute_variant")).not.toBeNull();
  });

  test("a D that stops or breaks before it asks ends it", async () => {
    for (const name of ["ended_refused", "unknown_drift", "fault_locked"]) {
      const f = await openStep({ ready: allMet(), run: runDoc("idle") });
      await clickStart(f);
      await serve(f, runDoc("launching"));
      await serve(f, runDoc(name));
      await wait(700);
      expect([name, sentKeys(f.state)]).toEqual([name, []]);
    }
  });

  test("a D that never asks ends it after the limit, with a note", async () => {
    const f = await openStep({
      ready: allMet(),
      run: runDoc("idle"),
      limits: { macroReleaseMs: 500 },
    });
    await clickStart(f);
    await serve(f, runDoc("launching"));
    await waitFor(() => $(f.host, "marvel0-autonomy-macro-ended") !== null, {
      label: "the timeout",
    });
    expect(sentKeys(f.state)).toEqual([]);
  });

  test("a refused execute is shown in words and is not sent again", async () => {
    const f = await openStep({ ready: allMet(), run: runDoc("idle") });
    f.state.answer = (call) =>
      call.op === "key"
        ? refusal(409, "monitor_not_green")
        : call.op === "challenge"
          ? { ok: true, status: 200, data: { challenge: "tok" } }
          : { ok: true, status: 202, data: {} };
    await clickStart(f);
    await serve(f, runDoc("await_phrase"));
    await waitFor(() => sentKeys(f.state).length === 1, { label: "execute" });
    await waitFor(() => $(f.host, "marvel0-autonomy-refused") !== null, {
      label: "the refusal",
    });
    expect(text($(f.host, "marvel0-autonomy-refused"))).toContain(
      E["marvel0.autonomy.err.monitor_not_green"],
    );
    await wait(700);
    expect(sentKeys(f.state)).toEqual(["execute"]);
  });
});
