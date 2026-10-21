import { afterEach, describe, expect, test } from "bun:test";
import { click, setupDom, waitFor } from "@/components/ds/__tests__/dom";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import { button, tick, tickAll } from "./actions-helpers";
import {
  $,
  dialog,
  keyButton,
  openStep,
  serve,
  text,
} from "./autonomy-helpers";
import { callsOf, readyDoc, reviewDoc, runDoc } from "./autonomy-fixtures";

// The second round of reviews of Step 5 (SAFETY N2, N3, N5, N6, N11): the page
// is run on the documents the reviewers recorded from the real backend
// functions (autonomy-review-docs.json) and on the samples changed the way the
// backend now changes them.

setupDom();
afterEach(() => {
  try {
    localStorage.clear();
  } catch {
    // no storage in this DOM
  }
});

const E = en as Record<string, string>;
const Z = zh as Record<string, string>;
const P = "marvel0.autonomy.";
const stop = (host: ParentNode) =>
  $(host, "marvel0-autonomy-stop") as HTMLButtonElement | null;
const caption = (host: ParentNode) =>
  text($(host, "marvel0-autonomy-stop-caption"));

describe("Stop when D did not answer and the backend offers no retry (SAFETY N2, probe P1)", () => {
  test("the adopted D of probe P1: the button is off, and the caption does not say 'one more try is offered'", async () => {
    const { host } = await openStep({
      run: reviewDoc("p1_after_ack_window"),
    });
    expect(stop(host)!.disabled).toBe(true);
    const frame = $(host, "marvel0-autonomy-stop-frame")!;
    expect(frame.getAttribute("data-mode")).toBe("unoffered");
    // The caption is the real reason, not the phase's promise.
    expect(caption(host)).toBe(
      `${E[`${P}stop.unoffered`]} ${E[`${P}stop.why.stop_spent`]} ${E[`${P}stop.unoffered_tail`]}`,
    );
    expect(caption(host)).not.toMatch(/One more try|it is the last/);
    expect(caption(host)).toContain("physical e-stop");
    expect(caption(host)).toContain("scripts/aeri_page_stop.py --again");
    // The sentence under the badge says the same thing: the arm may still be
    // moving, nothing is offered here (it never promised a retry).
    expect(text($(host, "marvel0-autonomy-hint"))).toBe(
      E[`${P}hint.orphaned_unconfirmed`],
    );
    expect($(host, "marvel0-autonomy-run")!.className).toContain(
      "rg-card--red",
    );
  });

  test("the same document in Chinese", async () => {
    const { host } = await openStep({
      run: reviewDoc("p1_after_ack_window"),
      zh: true,
    });
    expect(caption(host)).toBe(
      `${Z[`${P}stop.unoffered`]} ${Z[`${P}stop.why.stop_spent`]} ${Z[`${P}stop.unoffered_tail`]}`,
    );
    expect(caption(host)).not.toContain("再试一次");
  });

  test("the backend offers the retry for that state: the button is on and the caption is the retry's, once", async () => {
    const f = await openStep({
      run: reviewDoc("p1_after_ack_window", (doc) => {
        doc.stop.allowed = true;
        doc.stop.why_not = null;
      }),
    });
    expect(stop(f.host)!.disabled).toBe(false);
    expect(caption(f.host)).toBe(E[`${P}stop.retry`]);
    await click(stop(f.host));
    await tick(3, 10);
    expect(callsOf(f.state, "stop")).toHaveLength(1);
    expect(stop(f.host)!.disabled).toBe(true);
  });

  test("read at once after the adoption the Stop is 'sent, not confirmed', as it always was", async () => {
    const { host } = await openStep({ run: reviewDoc("p1_just_adopted") });
    expect(caption(host)).toBe(E[`${P}stop.sent`]);
    expect(stop(host)!.disabled).toBe(true);
  });

  test("the sentence under the badge of a stopping D whose retry is not offered does not promise one", async () => {
    const { host } = await openStep({
      run: runDoc("stopping_unconfirmed", (doc) => {
        doc.stop.allowed = false;
        doc.stop.why_not = { code: "stop_spent", params: {} };
      }),
    });
    expect(text($(host, "marvel0-autonomy-hint"))).toBe(
      E[`${P}hint.stopping_unoffered`],
    );
    expect(text($(host, "marvel0-autonomy-hint"))).not.toMatch(/One more try/);
    expect(stop(host)!.disabled).toBe(true);
  });

  test("the offered retry still says so under the badge", async () => {
    const { host } = await openStep({ run: runDoc("stopping_unconfirmed") });
    expect(text($(host, "marvel0-autonomy-hint"))).toBe(
      E[`${P}hint.stopping_unconfirmed`],
    );
    expect(stop(host)!.disabled).toBe(false);
  });
});

describe("Stop of a run another core owns, or whose site file is broken (SAFETY N2, probe P3)", () => {
  test("it is off, and the caption gives the reason in the Stop's own words", async () => {
    const { host } = await openStep({ run: reviewDoc("p3_site_broken") });
    expect(stop(host)!.disabled).toBe(true);
    expect(
      $(host, "marvel0-autonomy-stop-frame")!.getAttribute("data-mode"),
    ).toBe("off");
    expect(caption(host)).toBe(
      `${E[`${P}stop.off`]} ${E[`${P}stop.why.driven_elsewhere`]}`,
    );
    // Not the start refusal's sentence of the same code.
    expect(caption(host)).not.toContain(E[`${P}err.driven_elsewhere`]);
  });

  test("in Chinese", async () => {
    const { host } = await openStep({
      run: reviewDoc("p3_site_broken"),
      zh: true,
    });
    expect(caption(host)).toBe(
      `${Z[`${P}stop.off`]} ${Z[`${P}stop.why.driven_elsewhere`]}`,
    );
  });

  test("a D the backend calls gone (d_gone) while it is read as alive says that", async () => {
    const { host } = await openStep({
      run: runDoc("forward_running", (doc) => {
        doc.stop.allowed = false;
        doc.stop.why_not = { code: "d_gone", params: {} };
      }),
    });
    expect(caption(host)).toBe(
      `${E[`${P}stop.off`]} ${E[`${P}stop.why.d_gone`]}`,
    );
  });

  test("a reason with no sentence of its own is worded from the document", async () => {
    const { host } = await openStep({ run: runDoc("fault_locked") });
    expect(caption(host)).toContain(E[`${P}stop.off`]);
    expect(caption(host)).toContain("fault_locked");
  });

  test("a D the page cannot follow but may stop (the backend's unknown_unfollowed): Stop is on, and the sentence does not say it is refused", async () => {
    const { host } = await openStep({ run: runDoc("unknown_unfollowed") });
    expect(stop(host)!.disabled).toBe(false);
    expect(text($(host, "marvel0-autonomy-hint"))).toBe(
      E[`${P}hint.unknown_unfollowed`],
    );
    expect(text($(host, "marvel0-autonomy-hint"))).not.toMatch(
      /sends no Stop|cannot send a Stop/,
    );
    expect(caption(host)).toBe(E[`${P}stop.ready`]);
  });

  test("the dock says why its Stop is off too", async () => {
    const { host } = await openStep({ run: reviewDoc("p1_after_ack_window") });
    const dock = $(host, "marvel0-autonomy-dock")!;
    expect(dock === null).toBe(false);
    expect(
      ($(host, "marvel0-autonomy-dock-stop") as HTMLButtonElement).disabled,
    ).toBe(true);
    expect(text(dock)).toContain(E[`${P}stop.why.stop_spent`]);
    // The dock is small: it has the reason, not the way out that the card has.
    expect(text(dock)).not.toContain(E[`${P}stop.unoffered_tail`]);
    expect(
      $(host, "marvel0-autonomy-dock")!.querySelector(".rg-dock__note--why") ===
        null,
    ).toBe(false);
    expect(text(dock)).not.toContain(E[`${P}dock.note`]);
    expect(dock.getAttribute("data-mode")).toBe("unoffered");
  });
});

describe("hosting switched off in the control file while D runs (SAFETY N3)", () => {
  const off = (
    name: string,
    change?: (doc: ReturnType<typeof runDoc>) => void,
  ) =>
    runDoc(name, (doc) => {
      doc.enabled = false;
      change?.(doc);
    });

  test("the step says what is still true: the arm-moving keys are refused, Stop and Force-end stay, and nothing new can start", async () => {
    const { host } = await openStep({ run: off("await_release") });
    const note = $(host, "marvel0-autonomy-disabled")!;
    expect(text(note)).toBe(E[`${P}disabled.live`]);
    expect(note.getAttribute("data-live")).toBe("true");
    // Not the start-time sentence, which says that nothing acts.
    expect(text(note)).not.toContain("nothing on this step acts");
    expect(stop(host)!.disabled).toBe(false);
    expect($(host, "marvel0-autonomy-prepare-card") === null).toBe(true);
  });

  test("a key that moves the arm greys out with the reason, whatever the document still says; a key that moves nothing is left to the backend", async () => {
    const { host } = await openStep({ run: off("await_release") });
    const release = keyButton(host, "release_round")!;
    expect(release.disabled).toBe(true);
    expect(text(release.closest(".rg-akey"))).toContain(
      E[`${P}local.switched_off`],
    );
    // `quit` ends the run and moves nothing: it keeps its own state.
    expect(keyButton(host, "quit")!.disabled).toBe(false);
  });

  test("with the backend's own refusal (aeri_disabled) the key says that", async () => {
    const { host } = await openStep({
      run: off("await_release", (doc) => {
        const key = doc.keys.find(
          (k: { id: string }) => k.id === "release_round",
        );
        key.allowed = false;
        key.token = null;
        key.why_not = { code: "aeri_disabled", params: {} };
      }),
    });
    expect(keyButton(host, "release_round")!.disabled).toBe(true);
    expect(
      text(keyButton(host, "release_round")!.closest(".rg-akey")),
    ).toContain(E[`${P}err.aeri_disabled`]);
  });

  test("Force-end stays available when the wrap-up is stuck", async () => {
    const { host } = await openStep({ run: off("stalled") });
    const fold = $(host, "marvel0-autonomy-end") as HTMLDetailsElement;
    expect(button(fold, E[`${P}end.button`])!.disabled).toBe(false);
    expect(text($(host, "marvel0-autonomy-disabled"))).toBe(
      E[`${P}disabled.live`],
    );
  });

  test("switched off with no D, the start-time sentence stays (nothing acts then)", async () => {
    const { host } = await openStep({ run: runDoc("idle_disabled") });
    expect(text($(host, "marvel0-autonomy-disabled"))).toBe(
      E[`${P}disabled.off`],
    );
    expect(
      $(host, "marvel0-autonomy-disabled")!.getAttribute("data-live"),
    ).toBe(null);
  });

  test("the readiness document alone says it is off while D lives: the same sentence, and Stop is there", async () => {
    const { host } = await openStep({
      run: runDoc("forward_running"),
      ready: readyDoc("prepared_ready", (doc) => (doc.enabled = false)),
    });
    expect(text($(host, "marvel0-autonomy-disabled"))).toBe(
      E[`${P}disabled.live`],
    );
    expect(stop(host)!.disabled).toBe(false);
  });

  test("on again: the sentence goes and the keys come back", async () => {
    const f = await openStep({ run: off("await_release") });
    expect(keyButton(f.host, "release_round")!.disabled).toBe(true);
    await serve(f, runDoc("await_release"));
    expect($(f.host, "marvel0-autonomy-disabled") === null).toBe(true);
    await waitFor(() => !keyButton(f.host, "release_round")!.disabled, {
      label: "the key to come back",
    });
  });

  test("in Chinese", async () => {
    const { host } = await openStep({ run: off("await_release"), zh: true });
    expect(text($(host, "marvel0-autonomy-disabled"))).toBe(
      Z[`${P}disabled.live`],
    );
    expect(
      text(keyButton(host, "release_round")!.closest(".rg-akey")),
    ).toContain(Z[`${P}local.switched_off`]);
  });
});

describe("the release of a round asks what D asks (SAFETY N5)", () => {
  async function open(f: Awaited<ReturnType<typeof openStep>>) {
    await waitFor(() => !keyButton(f.host, "release_round")!.disabled, {
      label: "the release key to be on",
    });
    await click(keyButton(f.host, "release_round"));
  }
  const confirm = () =>
    Array.from(dialog()!.querySelectorAll("button")).find((b) =>
      text(b).startsWith(E[`${P}key.release_round`]),
    ) as HTMLButtonElement;

  test("one tick, D's own ask; Confirm is off until it is set; the hazards and the explanation are in the window", async () => {
    const f = await openStep({ run: runDoc("await_release") });
    await open(f);
    const boxes = dialog()!.querySelectorAll<HTMLInputElement>(
      "input[type=checkbox]:not([role=switch])",
    );
    expect(boxes).toHaveLength(1);
    expect(boxes[0].closest("label")!.textContent).toBe(
      E[`${P}dcheck.scene_ready`],
    );
    expect(boxes[0].closest("label")!.textContent).toBe(
      "The scene is set: objects placed, the area clear, a hand on the physical e-stop",
    );
    expect(confirm().disabled).toBe(true);
    expect(text(dialog())).toContain(E[`${P}hazard.release_round`]);
    expect(text(dialog())).toContain(E[`${P}confirm.checks_release`]);
    // Not the sentence about D's five and the stack's three.
    expect(text(dialog())).not.toContain(E[`${P}confirm.checks`]);
    await tickAll(dialog()!);
    expect(confirm().disabled).toBe(false);
    await click(confirm());
    await tick(3, 10);
    const sent = callsOf(f.state, "key");
    expect(sent).toHaveLength(1);
    expect(sent[0].action).toBe("release_round");
    expect(sent[0].attest).toEqual(["scene_ready"]);
    expect(sent[0]).not.toHaveProperty("confirm");
  });

  test("nothing is sent from the window until the tick is set", async () => {
    const f = await openStep({ run: runDoc("await_release") });
    await open(f);
    await click(confirm());
    expect(callsOf(f.state, "key")).toEqual([]);
  });

  test("in Chinese the tick and the note are Chinese", async () => {
    const f = await openStep({ run: runDoc("await_release"), zh: true });
    await open(f);
    const boxes = dialog()!.querySelectorAll<HTMLInputElement>(
      "input[type=checkbox]:not([role=switch])",
    );
    expect(boxes[0].closest("label")!.textContent).toBe(
      Z[`${P}dcheck.scene_ready`],
    );
    expect(text(dialog())).toContain(Z[`${P}confirm.checks_release`]);
  });
});

describe("the windows of the phrase quote what D printed above it (SAFETY N5)", () => {
  const SUMMARY = [
    "============================================================",
    "Run aeri-20261021-ext-r01: reviewed summary",
    "  rounds 5, forward steps 200",
    "============================================================",
  ];
  const UNCLEAN = [
    "Earlier policy switches did not end cleanly; their service state is not assumed.",
    "  /home/marvel/.aeri/swap/20261019-1.log: unit left running",
    "  /home/marvel/.aeri/swap/20261020-2.log: port 8000 still bound",
  ];
  const withTail = (name: string, lines: string[]) =>
    runDoc(name, (doc) => {
      doc.screen.tail = [...lines, doc.state.prompt.line];
    });
  async function open(f: Awaited<ReturnType<typeof openStep>>, id: string) {
    await waitFor(() => !keyButton(f.host, id)!.disabled, {
      label: `${id} to be on`,
    });
    await click(keyButton(f.host, id));
  }

  test("execute: the summary D printed is in the window, verbatim, with the prompt line apart under it", async () => {
    const f = await openStep({ run: withTail("await_phrase", SUMMARY) });
    await open(f, "execute");
    const context = $(dialog()!, "marvel0-autonomy-key-context")!;
    expect(context.tagName).toBe("PRE");
    expect(context.textContent).toBe(SUMMARY.join("\n"));
    expect(text($(dialog()!, "marvel0-autonomy-key-prompt"))).toBe(
      f.state.run!.state.prompt.line,
    );
    expect(text(dialog())).toContain(E[`${P}confirm.screen`]);
    // The prompt's own line is not quoted twice.
    expect(context.textContent).not.toContain(f.state.run!.state.prompt.line);
  });

  test("execute_variant: the logs of the earlier switches that did not end cleanly are in the window", async () => {
    const f = await openStep({
      run: withTail("await_phrase_variant", [...SUMMARY, ...UNCLEAN]),
    });
    await open(f, "execute_variant");
    const context = $(dialog()!, "marvel0-autonomy-key-context")!;
    for (const path of UNCLEAN.slice(1))
      expect(context.textContent).toContain(path);
    expect(context.textContent).toContain("reviewed summary");
  });

  test("a long screen is cut to its last 30 lines", async () => {
    const lines = Array.from({ length: 80 }, (_, i) => `earlier line ${i}`);
    const f = await openStep({ run: withTail("await_phrase", lines) });
    await open(f, "execute");
    const shown = $(dialog()!, "marvel0-autonomy-key-context")!.textContent!;
    expect(shown.split("\n")).toHaveLength(30);
    expect(shown.endsWith("earlier line 79")).toBe(true);
  });

  test("with nothing above the prompt there is no quote, only the prompt's line", async () => {
    const bare = await openStep({ run: runDoc("await_phrase") });
    await open(bare, "execute");
    expect($(dialog()!, "marvel0-autonomy-key-context") === null).toBe(true);
    expect($(dialog()!, "marvel0-autonomy-key-prompt") === null).toBe(false);
  });

  test("the other windows do not carry the quote (the release asks for a scene, not for a log)", async () => {
    const f = await openStep({
      run: runDoc("await_release", (doc) => {
        doc.screen.tail = ["some line", doc.state.prompt.line];
      }),
    });
    await waitFor(() => !keyButton(f.host, "release_round")!.disabled, {
      label: "the release key",
    });
    await click(keyButton(f.host, "release_round"));
    expect($(dialog()!, "marvel0-autonomy-key-context") === null).toBe(true);
  });

  test("the window follows a prompt that moves on: it is gone and nothing was sent", async () => {
    const f = await openStep({ run: withTail("await_phrase", SUMMARY) });
    await open(f, "execute");
    await tickAll(dialog()!);
    await serve(f, runDoc("services"));
    expect(dialog()).toBeNull();
    expect(callsOf(f.state, "key")).toEqual([]);
  });
});

describe("the product's service unit must give a hosted D time to wrap up (SAFETY N4, the readiness row unit_timeout)", () => {
  // The rows as the backend writes them (aeri_ready.row_unit): `ok` or `warn`,
  // never a refusal; `seconds` is the unit's TimeoutStopSec, or absent.
  const row = (
    level: string,
    params: Record<string, number | null>,
    text = "The server's English sentence",
  ) => ({ code: "unit_timeout", level, text, params });
  const open = (r: ReturnType<typeof row>, zh = false) =>
    openStep({
      run: runDoc("idle"),
      ready: readyDoc("prepared_ready", (doc) => doc.readiness.push(r)),
      zh,
    });
  const startButton = (host: ParentNode) =>
    $(host, "marvel0-autonomy-start") as HTMLButtonElement;
  const item = (host: ParentNode) =>
    host.querySelector('[data-code="unit_timeout"]') as HTMLElement;

  test("a unit that stops after 15 s: a warning that says N, what it cuts and what to use; Start stays on", async () => {
    const { host } = await open(row("warn", { need_s: 150, seconds: 15 }));
    expect(item(host).getAttribute("data-level")).toBe("warn");
    expect(text(item(host))).toContain(
      E[`${P}ready.unit_timeout`]
        .replace("{seconds}", "15")
        .replace("{need_s}", "150"),
    );
    expect(text(item(host))).toContain("levi stop");
    expect(text(item(host))).toContain("TimeoutStopSec");
    // It is advice, not a condition: the start is not held back by it.
    expect(startButton(host).disabled).toBe(false);
    // ... and it is in the list of warnings, not folded under what is met.
    expect(item(host).closest("details") === null).toBe(true);
  });

  test("a unit that could not be asked: the same advice without a number", async () => {
    const { host } = await open(row("warn", { need_s: 150, seconds: null }));
    expect(text(item(host))).toContain(
      E[`${P}ready.unit_timeout.unknown`].replace("{need_s}", "150"),
    );
    expect(text(item(host))).not.toContain("{");
    expect(startButton(host).disabled).toBe(false);
  });

  test("a unit that allows enough: met, folded", async () => {
    const { host } = await open(row("ok", { need_s: 150, seconds: 180 }));
    expect(item(host).getAttribute("data-level")).toBe("ok");
    expect(text(item(host))).toContain(
      E[`${P}ready.unit_timeout.ok`].replace("{need_s}", "150"),
    );
    expect(item(host).closest("details") === null).toBe(false);
  });

  test("in Chinese the warning is Chinese and says the same", async () => {
    const { host } = await open(
      row("warn", { need_s: 150, seconds: 15 }),
      true,
    );
    expect(text(item(host))).toContain(
      Z[`${P}ready.unit_timeout`]
        .replace("{seconds}", "15")
        .replace("{need_s}", "150"),
    );
    expect(item(host).querySelector("[lang=en]") === null).toBe(true);
  });
});
