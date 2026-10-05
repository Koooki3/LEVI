import { describe, expect, test } from "bun:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import { EpisodeList, RemoveDialog } from "../episode-list";
import { RemovedInLive } from "@/components/pool/facets-panel";
import { friendlyError } from "../friendly-error";
import {
  applyChange,
  changeNotice,
  removeEpisodes,
  restoreEpisodes,
  type Requester,
} from "../live-actions";
import {
  nameList,
  pruneSelection,
  removableDemos,
  removalBlock,
  rowSignature,
  shownDemos,
  toggleAll,
  toggleSelection,
} from "../live-logic";
import type { ChangeResult, DatasetDetail, DemoRow } from "../types";

const demo = (name: string, over: Partial<DemoRow> = {}): DemoRow => ({
  demo: name,
  state: "done",
  segments: 5,
  ...over,
});
const removed = (name: string, reason = "reflex"): DemoRow =>
  demo(name, { excluded: { at: 1_800_000_000, by: "person", reason } });
const detail = (over: Partial<DatasetDetail> = {}): DatasetDetail => ({
  enabled: true,
  name: "pi05__plates",
  demos: [demo("demo_0003"), demo("demo_0002"), demo("demo_0001")],
  excluded_count: 2,
  excluded_demos: [removed("demo_0005"), removed("demo_0004", "")],
  current: null,
  ...over,
});
const t = (text: string) => text;
const html = (props: Record<string, unknown> = {}, d = detail()) =>
  renderToStaticMarkup(
    createElement(EpisodeList, {
      dataset: "pi05__plates",
      detail: d,
      onChanged: () => {},
      ...props,
    }),
  );

describe("what can be removed", () => {
  test("only settled, mirrored episodes outside the batch in progress", () => {
    expect(removalBlock(demo("a"), null)).toBeNull();
    for (const state of ["mirrored", "done", "failed", "skipped_human"])
      expect(removalBlock(demo("a", { state }), [])).toBeNull();
    expect(removalBlock(demo("a"), ["a", "b"])).toBe("busy");
    expect(removalBlock(demo("a", { state: "rejected" }), [])).toBe("not_part");
    expect(removalBlock(demo("a", { state: "stuck" }), [])).toBe("not_part");
    // A removed one is not "blocked": it can be restored.
    expect(removalBlock(removed("a"), ["a"])).toBeNull();
    expect(
      removableDemos(
        [demo("a"), demo("b", { state: "rejected" }), demo("c")],
        ["c"],
      ),
    ).toEqual(["a"]);
  });

  test("the list toggles between the dataset and the removed ones", () => {
    const d = detail();
    expect(shownDemos(d, false).map((x) => x.demo)).toEqual([
      "demo_0003",
      "demo_0002",
      "demo_0001",
    ]);
    expect(shownDemos(d, true).map((x) => x.demo)).toEqual([
      "demo_0005",
      "demo_0004",
    ]);
    expect(shownDemos(undefined, true)).toEqual([]);
    expect(shownDemos({ ...d, excluded_demos: undefined }, true)).toEqual([]);
  });
});

describe("choosing several", () => {
  test("toggle, select all, and dropping what can no longer be chosen", () => {
    let chosen = toggleSelection(new Set(), "a");
    chosen = toggleSelection(chosen, "b");
    expect([...chosen]).toEqual(["a", "b"]);
    expect([...toggleSelection(chosen, "a")]).toEqual(["b"]);
    const all = ["a", "b", "c"];
    expect([...toggleAll(new Set(), all)]).toEqual(all);
    expect([...toggleAll(new Set(["a"]), all)]).toEqual(all);
    expect(toggleAll(new Set(all), all).size).toBe(0);
    expect(toggleAll(new Set(), []).size).toBe(0);
    // A batch started: "b" is no longer removable.
    expect([...pruneSelection(new Set(all), ["a", "c"])]).toEqual(["a", "c"]);
    expect(nameList(["a", "b", "c"], 2)).toBe("a, b +1");
    expect(nameList(["a"])).toBe("a");
  });

  test("a removal changes the row signature, so the detail is fetched again", () => {
    const row = { episodes: 3, pending: 0, annotating: 0, done: 3, failed: 0 };
    expect(rowSignature({ ...row, excluded: 1 })).not.toBe(rowSignature(row));
  });
});

describe("the calls", () => {
  const answer = (over: Partial<ChangeResult> = {}): ChangeResult => ({
    dataset: "pi05__plates",
    changed: ["demo_0001", "demo_0002"],
    unchanged: [],
    excluded_count: 2,
    ...over,
  });
  function recorder(reply: unknown | Error) {
    const calls: { method: string; path: string; body: unknown }[] = [];
    const request = (async (method: string, path: string, body?: unknown) => {
      calls.push({ method, path, body });
      if (reply instanceof Error) throw reply;
      return reply;
    }) as Requester;
    return { calls, request };
  }

  test("several episodes go in one request, the reason trimmed", async () => {
    const { calls, request } = recorder(answer());
    await removeEpisodes(
      "pi05__plates",
      ["demo_0001", "demo_0002"],
      "  hit the table ",
      request,
    );
    expect(calls).toEqual([
      {
        method: "POST",
        path: "live/datasets/pi05__plates/exclude",
        body: { demos: ["demo_0001", "demo_0002"], reason: "hit the table" },
      },
    ]);
  });

  test("a dataset name with a suffix is one path segment", async () => {
    const { calls, request } = recorder(answer());
    await removeEpisodes(
      "pi05__plates__at__root-2",
      ["demo_0001"],
      "",
      request,
    );
    await restoreEpisodes("a/b@c", ["demo_0001"], request);
    expect(calls[0].path).toBe(
      "live/datasets/pi05__plates__at__root-2/exclude",
    );
    expect(calls[1].path).toBe("live/datasets/a%2Fb%40c/restore");
  });

  test("restoring posts the names and nothing else", async () => {
    const { calls, request } = recorder(answer());
    await restoreEpisodes("pi05__plates", ["demo_0004"], request);
    expect(calls[0].path).toBe("live/datasets/pi05__plates/restore");
    expect(calls[0].body).toEqual({ demos: ["demo_0004"] });
  });

  test("a confirmed removal ends in a sentence about what changed", async () => {
    const { request } = recorder(answer({ review_hidden: ["review-1"] }));
    const outcome = await applyChange(
      "remove",
      "pi05__plates",
      ["demo_0001", "demo_0002"],
      "",
      t,
      request,
    );
    expect(outcome.ok).toBe(true);
    if (outcome.ok)
      expect(outcome.notice).toBe(
        "2 episode(s) removed (restorable) · 1 review run(s) no longer counted",
      );
  });

  test("a restore says how many were not removed", () => {
    expect(
      changeNotice(
        "restore",
        answer({ changed: ["a"], unchanged: ["b", "c"] }),
        t,
      ),
    ).toBe("1 episode(s) restored · 2 were not removed");
    expect(
      changeNotice("remove", answer({ changed: [], unchanged: ["a"] }), t),
    ).toBe("0 episode(s) removed (restorable) · 1 already removed");
  });

  test("a refusal comes back as a message, with its plain-words form", async () => {
    const busy =
      "demo_0001: being labelled (part of the batch in progress); try again after the batch";
    const { request } = recorder(new Error(busy));
    const outcome = await applyChange(
      "remove",
      "pi05__plates",
      ["demo_0001"],
      "",
      t,
      request,
    );
    expect(outcome).toEqual({ ok: false, message: busy });
    expect(friendlyError(busy, t)).toStartWith("Being labelled right now");
    expect(
      friendlyError(
        "demo_0009: was never taken into the dataset, nothing to remove",
        t,
      ),
    ).toStartWith("This episode was never taken");
    expect(friendlyError("Unknown episode: demo_0042", t)).toBe(
      "Unknown episode: demo_0042",
    );
  });
});

describe("the list", () => {
  test("shows the episodes with a way to choose and remove them", () => {
    const out = html();
    expect(out).toContain("Episodes (newest first)");
    for (const name of ["demo_0003", "demo_0002", "demo_0001"])
      expect(out).toContain(name);
    expect(out).not.toContain("demo_0005");
    expect(out).toContain("Removed (2)");
    expect(out).toContain("Select all");
    expect(out).toContain('aria-pressed="false"');
    expect(out).not.toContain("Remove selected");
  });

  test("a verdict under the terminal-aware rule says why, in both languages", () => {
    const verdict = {
      outcome: "failure",
      events: 2,
      valid_events: 1,
      rule: "last_valid_not_regrasped",
      closes_after_last_valid: 1,
      place_outcome: "success",
    };
    const out = html(
      {},
      detail({ demos: [demo("demo_0003", { verdict }), demo("demo_0002")] }),
    );
    expect(out).toContain("(1/2)");
    expect(out).toContain("the gripper closed again after the last release");
    // The default rule's row has no reason.
    const plain = html(
      {},
      detail({
        demos: [
          demo("demo_0003", { verdict: { outcome: "failure", events: 1 } }),
        ],
      }),
    );
    expect(plain).not.toContain("closed again");
    for (const key of [
      "the gripper closed again after the last release",
      "the last placement is a failure",
      "no placement time segment",
      "the last placement is undecided",
      "placement not checked: no time segments",
      "no gripper close frames recorded: a new grasp cannot be checked",
    ]) {
      expect((en as Record<string, string>)[key]).toBe(key);
      expect((zh as Record<string, string>)[key]).toBeTruthy();
      expect((zh as Record<string, string>)[key]).not.toBe(key);
    }
  });

  test("an episode of the batch in progress cannot be chosen, and says why", () => {
    const out = html({}, detail({ current: { demos: ["demo_0002"] } }));
    const box = (name: string) =>
      out.match(
        new RegExp(`<input[^>]*aria-label="Select ${name}"[^>]*>`),
      )?.[0] ?? "";
    expect(box("demo_0002")).toContain("disabled");
    expect(box("demo_0001")).not.toContain("disabled");
    // The reason is words in the row, tied to the control: not a `title`.
    expect(box("demo_0002")).not.toContain("title=");
    const described = box("demo_0002").match(/aria-describedby="([^"]+)"/);
    expect(described).not.toBeNull();
    expect(out).toMatch(
      new RegExp(
        `<span id="${described![1].replace(/:/g, "\\:")}"[^>]*>Being labelled now`,
      ),
    );
    expect(box("demo_0001")).not.toContain("aria-describedby");
    const rejected = html(
      {},
      detail({ demos: [demo("demo_0009", { state: "rejected" })] }),
    );
    expect(rejected).toContain("Never taken into the dataset");
  });

  test("a selection offers a bulk removal with its count", () => {
    const out = html({ initialSelected: ["demo_0001", "demo_0003"] });
    expect(out).toContain("Remove selected");
    expect(out).toContain("(2)");
    expect(out).toContain("Clear selection");
    expect((out.match(/checked=""/g) ?? []).length).toBeGreaterThanOrEqual(2);
  });

  test("with more than ten rows only the shown ones can be selected", () => {
    const many = detail({
      demos: Array.from({ length: 12 }, (_, i) =>
        demo(`demo_${String(i).padStart(4, "0")}`),
      ),
    });
    const out = html({}, many);
    expect(out).toContain("Select the shown");
    expect(out).not.toContain("Select all");
    expect(out).toContain("Show all 12");
    expect(out).toContain('aria-label="Select demo_0009"');
    expect(out).not.toContain('aria-label="Select demo_0010"');
    expect(html({}, detail())).toContain("Select all");
  });

  test("the removed view lists them with their reason and a Restore", () => {
    const out = html({ initialShowRemoved: true });
    expect(out).toContain("Removed episodes (not counted, not labelled)");
    expect(out).toContain("demo_0005");
    expect(out).toContain("reflex");
    expect(out).toContain("no reason given"); // demo_0004 gave none
    expect(out).not.toContain("demo_0003");
    expect(out).toContain("Back to the episodes");
    expect((out.match(/>Restore</g) ?? []).length).toBe(2);
    const chosen = html({
      initialShowRemoved: true,
      initialSelected: ["demo_0005"],
    });
    expect(chosen).toContain("Restore selected");
    expect(chosen).not.toContain("Remove selected");
  });

  test("nothing removed yet says so; nothing at all shows nothing", () => {
    const none = html(
      { initialShowRemoved: true },
      detail({ excluded_count: 0, excluded_demos: [] }),
    );
    expect(none).toContain("No episode has been removed.");
    expect(
      html({}, detail({ demos: [], excluded_count: 0, excluded_demos: [] })),
    ).toBe("");
    expect(
      renderToStaticMarkup(
        createElement(EpisodeList, {
          dataset: "x",
          detail: undefined,
          onChanged: () => {},
        }),
      ),
    ).toBe("");
    const all = html({}, detail({ demos: [] }));
    expect(all).toContain("Every episode has been removed.");
  });
});

describe("the confirmation", () => {
  const dialog = (demos: string[], error = "") =>
    renderToStaticMarkup(
      createElement(RemoveDialog, {
        demos,
        reason: "hit the table",
        onReason: () => {},
        busy: false,
        error,
        onConfirm: () => {},
        onCancel: () => {},
      }),
    );

  test("says what a removal does and does not do", () => {
    const out = dialog(["demo_0001"]);
    expect(out).toContain("Remove episode (restorable)");
    expect(out).toContain("Nothing is deleted");
    expect(out).toContain(
      "the rollout folders and the copies LEVI mirrored stay",
    );
    expect(out).toContain("annotations already written stay too");
    expect(out).toContain("Removed list of this card");
    expect(out).toContain("Reason (optional)");
    expect(out).toContain('value="hit the table"');
    expect(out).toContain("Cancel");
  });

  test("a bulk removal names the first few and always gives the total", () => {
    const out = dialog(["demo_0001", "demo_0002", "demo_0003"]);
    expect(out).toContain("Remove episodes (restorable)");
    expect(out).toContain("demo_0001, demo_0002, demo_0003");
    expect(out).toContain("3 episodes in total");
    const names = Array.from({ length: 40 }, (_, i) => `demo_${i}`);
    const long = dialog(names);
    expect(long).toContain("+34");
    expect(long).toContain("40 episodes in total");
    expect(dialog(["demo_0001"])).toContain("1 episodes in total");
  });

  test("a refusal is shown in plain words inside it", () => {
    const out = dialog(
      ["demo_0001"],
      "demo_0001: being labelled (part of the batch in progress); try again after the batch",
    );
    expect(out).toContain("Being labelled right now");
  });

  test("the list opens it for what was chosen", () => {
    const out = html({ initialPending: ["demo_0001", "demo_0002"] });
    expect(out).toContain("Remove episodes (restorable)");
    expect(out).toContain("<dialog");
  });
});

describe("both languages", () => {
  const keys = [
    "Remove episode (restorable)",
    "Remove episodes (restorable)",
    "Removed ({n})",
    "Restore",
    "Restore selected",
    "Remove selected",
    "Reason (optional)",
    "Back to the episodes",
    "Removed episodes (not counted, not labelled)",
    "{n} episode(s) removed (restorable)",
    "{n} episode(s) restored",
    "{n} already removed",
    "{n} were not removed",
    "{n} review run(s) no longer counted",
    "{n} episodes in total",
    "Select the shown",
    "Episodes removed on the live page",
    "Being labelled now: part of the batch in progress",
    "Never taken into the dataset",
    "removed by a person (not counted)",
    "Removed on the live page",
    "read-only except removing episodes",
    "They stop being counted and labelled, and the training pool leaves them out. Nothing is deleted: the rollout folders and the copies LEVI mirrored stay, and annotations already written stay too.",
    "You can bring them back at any time from the Removed list of this card.",
    "Being labelled right now (part of the batch in progress). Try again when the batch has finished; nothing was removed.",
  ];
  test("every new sentence is translated", () => {
    for (const key of keys) {
      expect((en as Record<string, string>)[key]).toBe(key);
      expect((zh as Record<string, string>)[key]).toBeTruthy();
      expect((zh as Record<string, string>)[key]).not.toBe(key);
    }
    // Placeholders survive translation.
    for (const key of keys.filter((k) => k.includes("{n}")))
      expect((zh as Record<string, string>)[key]).toContain("{n}");
    expect((zh as Record<string, string>)["Remove episode (restorable)"]).toBe(
      "删除片段（可恢复）",
    );
  });
});

describe("the pool page says why it shows fewer", () => {
  test("the number of recordings removed on the live page, or nothing", () => {
    const out = renderToStaticMarkup(
      createElement(RemovedInLive, { count: 1234 }),
    );
    expect(out).toContain("Episodes removed on the live page");
    expect(out).toContain("1,234");
    expect(out).toContain("not listed, counted or exported");
    expect(
      renderToStaticMarkup(createElement(RemovedInLive, { count: 0 })),
    ).toBe("");
    expect(renderToStaticMarkup(createElement(RemovedInLive, {}))).toBe("");
  });
});
