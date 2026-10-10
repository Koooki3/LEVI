import {
  click,
  fire,
  flush,
  focus,
  render,
  setupDom,
  waitFor,
} from "@/components/ds/__tests__/dom";
import { beforeEach, describe, expect, mock, test } from "bun:test";
import { act } from "react";
import type { DatasetIdent } from "@/utils/annotationsClient";
import type {
  RecapComparison,
  RecapEpisode,
  RecapJob,
  RecapRevision,
  RecapRevisions,
  RecapRunRequest,
  RecapStatus,
  RecapSummary,
} from "@/types/recap.types";

setupDom();

const r1 = "20261008-120000";
const r2 = "20261009-120000";
const revision = (
  id: string,
  more: Partial<RecapRevision> = {},
): RecapRevision => ({
  revision_id: id,
  checkpoint: id === r1 ? "value-r1-step3000" : "value-r2-step6000",
  provider: "rlinf",
  step: id === r1 ? 3000 : 6000,
  created_at: 1790000000,
  episodes: 12,
  frames: 1673,
  threshold: id === r1 ? 0.00749 : 0.005278945887678077,
  threshold_source: "checkpoint",
  positive_quantile: 0.3,
  lookahead: 10,
  positive_fraction: 0.3,
  return_min: id === r1 ? -1000 : -799,
  return_max: 0,
  stale: false,
  current: id === r2,
  dataset_type: "rollout",
  gamma: 1,
  failure_reward: id === r1 ? -2 : -1,
  precision: id === r1 ? "fp32" : "bf16",
  value_support: { num_bins: id === r1 ? 201 : 511, v_min: -1, v_max: 0 },
  ...more,
});
const rows = () => [revision(r2), revision(r1)];
const episode = (id: string, ep = 0, value?: number): RecapEpisode => ({
  episode_index: ep,
  revision_id: id,
  fps: 10,
  threshold: revision(id).threshold,
  frame_index: [0, 1, 2],
  timestamp: [0, 0.1, 0.2],
  value:
    value == null
      ? id === r1
        ? [-0.8, -0.6, -0.4]
        : [-0.4, -0.3, -0.2]
      : [value, value, value],
  advantage: [0.01, 0.02, -0.01],
  positive: id === r1 ? [false, true, false] : [true, false, true],
});
const status = (): RecapStatus => ({
  checkpoints: [
    {
      name: "value-r1-step3000",
      provider: "rlinf",
      ready: true,
      reason: null,
      step: 3000,
      created_at: 1790000000,
      notes: null,
    },
    {
      name: "value-r2-step6000",
      provider: "rlinf",
      ready: true,
      reason: null,
      step: 6000,
      created_at: 1790000000,
      notes: null,
    },
  ],
  worker: { ready: true, reason: null },
  current: revision(r2),
  job: null,
});
const job: RecapJob = {
  id: "test-job",
  checkpoint: "value-r2-step6000",
  repo_id: "local/demo",
  status: "queued",
  progress: { stage: "values", done: 0, total: 3 },
  error: null,
  revision_id: null,
  created_at: 1790000000,
};
const compare = (a: string, b: string): RecapComparison => ({
  dataset: "demo",
  a: revision(a),
  b: revision(b),
  episodes: { shared: 1, only_a: 1, only_b: 2 },
  frames: { shared: 3, only_a: 1, only_b: 2 },
  labels: {
    agreement: 0,
    positive_a_only: 2,
    positive_b_only: 1,
    both_positive: 0,
    both_negative: 0,
    positive_fraction_a: 2 / 3,
    positive_fraction_b: 1 / 3,
  },
  value: { mean_a: -0.3, mean_b: -0.6, mean_abs_diff: 0.3, corr: 1 },
  advantage: { mean_a: 0.02, mean_b: 0.01, mean_abs_diff: 0.01, corr: 0.5 },
  value_return_units: {
    mean_a: -200,
    mean_b: -300,
    mean_abs_diff: 100,
    corr: 1,
  },
  per_episode: [
    {
      episode: 0,
      outcome: "success",
      frames: 3,
      label_agreement: 0,
      positive_fraction_a: 2 / 3,
      positive_fraction_b: 1 / 3,
      mean_value_a: -0.3,
      mean_value_b: -0.6,
      value_mean_abs_diff: 0.3,
      value_corr: 1,
    },
  ],
  notes: ["return_range_differs", "threshold_differs"],
  outcome_separation: null,
});

let context = {
  episodeId: 0,
  ident: { repoId: "local/demo" },
  readOnly: false,
};
let statusHandler: (ident: DatasetIdent) => Promise<RecapStatus> = async () =>
  status();
let revisionsHandler: (
  ident: DatasetIdent,
) => Promise<RecapRevisions> = async () => ({ current: r2, revisions: rows() });
let episodeHandler: (
  ep: number,
  ident: DatasetIdent,
  signal?: AbortSignal,
  rid?: string,
  version?: string | null,
) => Promise<RecapEpisode | null> = async (ep, _ident, _signal, rid) =>
  episode(rid!, ep);
let compareHandler: (
  ident: DatasetIdent,
  a: string,
  b: string,
  signal?: AbortSignal,
  versions?: { a?: string | null; b?: string | null },
) => Promise<RecapComparison> = async (_ident, a, b) => compare(a, b);
let summaryHandler: (
  ident: DatasetIdent,
  rid?: string,
) => Promise<RecapSummary | null> = async () => null;
const fetchEpisode = mock(
  (
    ep: number,
    ident: DatasetIdent,
    signal?: AbortSignal,
    rid?: string,
    version?: string | null,
  ) => episodeHandler(ep, ident, signal, rid, version),
);
const fetchCompare = mock(
  (
    ident: DatasetIdent,
    a: string,
    b: string,
    signal?: AbortSignal,
    versions?: { a?: string | null; b?: string | null },
  ) => compareHandler(ident, a, b, signal, versions),
);
class RecapRecomputedError extends Error {}
const run = mock(async (ident: DatasetIdent, request: RecapRunRequest) => {
  void ident;
  void request;
  return job;
});
const cancel = mock(async () => ({ ...job, status: "cancelled" as const }));

mock.module("@/context/annotations-context", () => ({
  useAnnotations: () => context,
}));
mock.module("@/utils/annotationsClient", () => ({
  RecapRecomputedError,
  isAnnotateBackendEnabled: () => true,
  fetchRecapStatus: (ident: DatasetIdent) => statusHandler(ident),
  fetchRecapRevisions: (ident: DatasetIdent) => revisionsHandler(ident),
  fetchRecapEpisode: fetchEpisode,
  fetchRecapSummary: (
    ident: DatasetIdent,
    _signal?: AbortSignal,
    rid?: string,
  ) => summaryHandler(ident, rid),
  fetchRecapCompare: fetchCompare,
  fetchRecapJob: async () => job,
  runRecap: run,
  cancelRecapJob: cancel,
}));

import { RecapValueSection } from "@/components/recap-value-section";
import { LocaleProvider, useLocale } from "@/components/levi-locale";

const seek = mock((time: number) => {
  void time;
});
function LanguageSwitch() {
  const { setLanguage } = useLocale();
  return (
    <button onClick={() => setLanguage("zh")} data-language="zh">
      中文
    </button>
  );
}
function page() {
  return (
    <LocaleProvider>
      <LanguageSwitch />
      <div className="annotations-skin">
        <RecapValueSection
          duration={1}
          currentTime={0}
          onSeek={seek}
          onBandClick={() => undefined}
          onHoverMove={() => undefined}
          onHoverLeave={() => undefined}
          showTip={() => undefined}
          moveTip={() => undefined}
          hideTip={() => undefined}
        />
      </div>
    </LocaleProvider>
  );
}
const selectors = (host: HTMLElement) => [
  ...host.querySelectorAll<HTMLSelectElement>(".recap-version-controls select"),
];
const choose = async (select: HTMLSelectElement, id: string) => {
  select.value = id;
  await fire(select, new Event("change", { bubbles: true }));
};
const computePicker = (host: HTMLElement) =>
  host.querySelector<HTMLSelectElement>(
    'select[aria-label="Value-model checkpoint for computation"]',
  )!;
const button = (host: HTMLElement, text: string) =>
  [...host.querySelectorAll<HTMLButtonElement>("button")].find(
    (node) => node.textContent === text,
  )!;
async function loaded(host: HTMLElement) {
  await waitFor(
    () => selectors(host)[0]?.value === r2 && host.querySelector(".recap-line"),
    { label: "current revision and curve" },
  );
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((accept) => {
    resolve = accept;
  });
  return { promise, resolve };
}

beforeEach(() => {
  context = { episodeId: 0, ident: { repoId: "local/demo" }, readOnly: false };
  statusHandler = async () => status();
  revisionsHandler = async () => ({ current: r2, revisions: rows() });
  episodeHandler = async (ep, _ident, _signal, rid) => episode(rid!, ep);
  compareHandler = async (_ident, a, b) => compare(a, b);
  summaryHandler = async () => null;
  fetchEpisode.mockClear();
  fetchCompare.mockClear();
  run.mockClear();
  cancel.mockClear();
  seek.mockClear();
  window.localStorage.clear();
});

describe("saved Value model results", () => {
  test("initial loading waits for status to publish completed worker output before listing revisions", async () => {
    const completion = deferred<RecapStatus>();
    const nextId = "20261009-180000";
    const nextRevision = revision(nextId, {
      checkpoint: "value-step9000",
      step: 9000,
      current: true,
    });
    const events: string[] = [];
    let published = false;
    statusHandler = async () => {
      events.push("status started");
      const next = await completion.promise;
      published = true;
      events.push("worker output published");
      return next;
    };
    revisionsHandler = async () => {
      events.push("revisions read");
      return published
        ? { current: nextId, revisions: [nextRevision] }
        : { current: null, revisions: [] };
    };
    const { host } = await render(page());
    expect(events).toEqual(["status started"]);
    await act(async () =>
      completion.resolve({
        ...status(),
        current: nextRevision,
        job: { ...job, status: "succeeded", revision_id: nextId },
      }),
    );
    await waitFor(
      () =>
        selectors(host)[0]?.value === nextId &&
        host.querySelector(".recap-line"),
    );
    expect(events).toEqual([
      "status started",
      "worker output published",
      "revisions read",
    ]);
    expect(
      host.querySelector(".recap-version-facts.side-a")?.textContent,
    ).toContain("value-step9000");
    expect(host.textContent).not.toContain("No saved value-model results yet");
    expect(fetchEpisode.mock.calls.some((call) => call[3] === nextId)).toBe(
      true,
    );
  });

  test("a status failure still permits reading saved history", async () => {
    statusHandler = async () => {
      throw new Error("worker status unavailable");
    };
    const { host } = await render(page());
    await loaded(host);
    expect(host.textContent).toContain("worker status unavailable");
    expect(selectors(host)).toHaveLength(2);
    expect(host.querySelector(".recap-line")).not.toBeNull();
  });
  test("defaults to current with full identities; browsing and computing have independent selections", async () => {
    const { host } = await render(page());
    await loaded(host);
    const [primary, secondary] = selectors(host);
    expect(primary.options[0].textContent).toContain("value-r2-step6000");
    expect(primary.options[0].textContent).toContain("step 6000");
    expect(primary.options[0].textContent).toContain("computed");
    expect(primary.options[0].textContent).toContain("2026");
    expect(primary.options[0].textContent).toContain("12 episodes");
    // The run id moved from the picker into the details.
    expect(primary.options[0].textContent).not.toContain(r2);
    expect(
      host.querySelector(".recap-version-facts.side-a")!.textContent,
    ).toContain(r2);
    expect(primary.labels?.[0]?.textContent).toBe("Saved result A");
    expect(secondary.labels?.[0]?.textContent).toBe("Compare with result B");
    await focus(primary);
    expect(document.activeElement).toBe(primary);
    await click(button(host, "Recompute"));
    expect(computePicker(host).value).toBe("value-r2-step6000");
    await choose(primary, r1);
    await waitFor(() =>
      host
        .querySelector(".recap-frame-readout")
        ?.textContent?.includes("V -0.800"),
    );
    expect(computePicker(host).value).toBe("value-r2-step6000");
    expect(run).not.toHaveBeenCalled();
    expect(fetchEpisode.mock.calls.some((call) => call[3] === r1)).toBe(true);
    expect(host.textContent).toContain("1673");
  });

  test("overlays two Value curves, keeps two label rows, shows coverage and exact parameter boundaries", async () => {
    const { host } = await render(page());
    await loaded(host);
    await choose(selectors(host)[1], r1);
    await waitFor(() => host.querySelector(".recap-comparison"));
    expect(host.querySelectorAll(".recap-advantage-row")).toHaveLength(2);
    expect(host.querySelectorAll(".recap-line")).toHaveLength(2);
    expect(host.querySelector(".recap-line.comparison")).not.toBeNull();
    const text = host.textContent!;
    expect(text).toContain("0.005278945887678077");
    expect(text).toContain("0.00749");
    const aFacts = host.querySelector(
      ".recap-version-facts.side-a",
    )!.textContent!;
    const bFacts = host.querySelector(
      ".recap-version-facts.side-b",
    )!.textContent!;
    expect(aFacts).toContain("Discount factor 1");
    expect(aFacts).toContain("Failure penalty -1");
    expect(aFacts).toContain("Inference precision bf16");
    expect(aFacts).toContain("Value bins 511 [-1, 0]");
    expect(bFacts).toContain("Failure penalty -2");
    expect(bFacts).toContain("Inference precision fp32");
    expect(bFacts).toContain("Value bins 201 [-1, 0]");
    expect(text).toContain("Shared frame coverage");
    expect(text).toContain("75.0%");
    expect(text).toContain("60.0%");
    expect(text).toContain("Label flips");
    expect(text).toContain("Continuous advantage");
    expect(text).toContain("Value in return units");
    expect(text).toContain(
      "normalized Value and advantage are not on the same original-return scale",
    );
    expect(text).not.toContain("return_range_differs");
    const mark = host.querySelector<HTMLButtonElement>(".tl-seg.adv")!;
    expect(mark.type).toBe("button");
    expect(mark.getAttribute("aria-label")).toContain("positive advantage");
    await focus(mark);
    await click(mark);
    expect(seek).toHaveBeenCalledWith(0);
    await choose(selectors(host)[0], r1);
    expect(selectors(host)[1].value).toBe("");
    expect(host.querySelector(".recap-comparison")).toBeNull();
    expect(host.querySelector(".recap-line.comparison")).toBeNull();
    expect(
      host.querySelectorAll(".recap-advantage-row.side-b .tl-seg"),
    ).toHaveLength(0);
    expect(host.querySelector(".recap-frame-readout.side-b")).toBeNull();
  });

  test("a comparison version missing the episode stays empty instead of borrowing the current result", async () => {
    episodeHandler = async (ep, _ident, _signal, rid) =>
      rid === r1 ? null : episode(rid!, ep);
    const { host } = await render(page());
    await loaded(host);
    await choose(selectors(host)[1], r1);
    await waitFor(() =>
      host
        .querySelector(".recap-advantage-row.side-b")
        ?.textContent?.includes("No advantage labels for this episode"),
    );
    expect(host.querySelectorAll(".recap-line")).toHaveLength(1);
    expect(
      host.querySelector(".recap-frame-readout.side-b")?.textContent,
    ).toContain("No labelled frame");
    expect(
      host.querySelector(".recap-version-facts.side-b")?.textContent,
    ).toContain(r1);
  });

  test("late version reads cannot overwrite the newly selected version, even if transport ignores abort", async () => {
    const old = deferred<RecapEpisode | null>();
    episodeHandler = async (ep, _ident, _signal, rid) =>
      rid === r1 ? old.promise : episode(rid!, ep);
    const { host } = await render(page());
    await loaded(host);
    await choose(selectors(host)[0], r1);
    const signal = fetchEpisode.mock.calls.find((call) => call[3] === r1)![2]!;
    expect(host.querySelector(".recap-line")).toBeNull();
    await choose(selectors(host)[0], r2);
    expect(signal.aborted).toBe(true);
    await waitFor(() =>
      host
        .querySelector(".recap-frame-readout")
        ?.textContent?.includes("V -0.400"),
    );
    await act(async () => old.resolve(episode(r1, 0, -0.99)));
    expect(host.querySelector(".recap-frame-readout")?.textContent).toContain(
      "V -0.400",
    );
    expect(host.textContent).not.toContain("-0.990");
  });

  test("episode changes preserve selected revisions and cancel the previous episode reads", async () => {
    const old = deferred<RecapEpisode | null>();
    episodeHandler = async (ep, _ident, _signal, rid) =>
      ep === 1 ? old.promise : episode(rid!, ep, ep === 2 ? -0.25 : undefined);
    const { host, rerender } = await render(page());
    await loaded(host);
    await choose(selectors(host)[0], r1);
    context = { ...context, episodeId: 1 };
    await rerender(page());
    const signal = fetchEpisode.mock.calls.find((call) => call[0] === 1)![2]!;
    expect(host.querySelector(".recap-line")).toBeNull();
    context = { ...context, episodeId: 2 };
    await rerender(page());
    expect(signal.aborted).toBe(true);
    await waitFor(() =>
      host
        .querySelector(".recap-frame-readout")
        ?.textContent?.includes("V -0.250"),
    );
    await act(async () => old.resolve(episode(r1, 1, -0.99)));
    expect(selectors(host)[0].value).toBe(r1);
    expect(host.querySelector(".recap-frame-readout")?.textContent).toContain(
      "V -0.250",
    );
  });

  test("switching datasets clears old result identities and never shows their curves", async () => {
    const { host, rerender } = await render(page());
    await loaded(host);
    await choose(selectors(host)[1], r1);
    statusHandler = async () => ({ ...status(), current: null });
    revisionsHandler = async () => ({ current: null, revisions: [] });
    context = { ...context, ident: { repoId: "local/new" } };
    await rerender(page());
    await waitFor(() =>
      host.textContent?.includes("No saved value-model results yet"),
    );
    expect(selectors(host)).toHaveLength(0);
    expect(host.querySelectorAll(".recap-line")).toHaveLength(0);
    expect(host.textContent).not.toContain(r1);
    expect(host.textContent).not.toContain(r2);
  });

  test("network errors are distinct from an uncomputed episode and refresh can recover", async () => {
    episodeHandler = async () => {
      throw new Error("episode proxy down");
    };
    const { host } = await render(page());
    await waitFor(() => host.textContent?.includes("episode proxy down"));
    expect(host.querySelector(".recap-advantage-row")?.textContent).toContain(
      "Result could not be loaded",
    );
    expect(
      host.querySelector(".recap-advantage-row")?.textContent,
    ).not.toContain("No advantage labels");
    episodeHandler = async (ep, _ident, _signal, rid) => episode(rid!, ep);
    await click(button(host, "Refresh results"));
    await waitFor(() => host.querySelector(".recap-line"));
    expect(host.textContent).not.toContain("episode proxy down");
    compareHandler = async () => {
      throw new Error("source frames changed");
    };
    await choose(selectors(host)[1], r1);
    await waitFor(() => host.textContent?.includes("source frames changed"));
    expect(host.textContent).toContain("Could not compare these results");
    expect(host.querySelector(".recap-comparison")).toBeNull();
    expect(host.querySelector(".recap-line.comparison")).toBeNull();
    expect(
      host.querySelectorAll(".recap-advantage-row.side-b .tl-seg"),
    ).toHaveLength(0);
    expect(
      host.querySelector(".recap-frame-readout.side-b")?.textContent,
    ).toContain("Comparison unavailable");
  });

  test("a pairing must be checked before overlay and a cancelled pairing cannot replace its successor", async () => {
    const old = deferred<RecapComparison>();
    compareHandler = async (_ident, a, b) =>
      a === r2
        ? old.promise
        : {
            ...compare(a, b),
            value: {
              mean_a: -0.4,
              mean_b: -0.5,
              mean_abs_diff: 0.1234,
              corr: 1,
            },
          };
    const { host } = await render(page());
    await loaded(host);
    await choose(selectors(host)[1], r1);
    expect(host.querySelector(".recap-line.comparison")).toBeNull();
    expect(host.textContent).toContain("Checking comparison…");
    const signal = fetchCompare.mock.calls[0][3]!;
    await choose(selectors(host)[0], r1);
    expect(signal.aborted).toBe(true);
    await choose(selectors(host)[1], r2);
    await waitFor(() =>
      host.querySelector(".recap-comparison")?.textContent?.includes("0.1234"),
    );
    await act(async () => old.resolve(compare(r2, r1)));
    expect(host.querySelector(".recap-comparison")?.textContent).toContain(
      "0.1234",
    );
    expect(selectors(host)[0].value).toBe(r1);
    expect(selectors(host)[1].value).toBe(r2);
  });

  test("one result remains browseable with a visible reason comparison is disabled", async () => {
    revisionsHandler = async () => ({ current: r2, revisions: [revision(r2)] });
    const { host } = await render(page());
    await loaded(host);
    expect(selectors(host)[1].disabled).toBe(true);
    expect(host.textContent).toContain(
      "Save another result to enable comparison",
    );
  });

  test("the compute controls choose no label rule: they show the automatic one and send none", async () => {
    statusHandler = async () => ({
      ...status(),
      current: null,
      dataset_type: {
        setting: "auto",
        dataset_type: "rollout",
        source: "fallback",
        reason: "no dataset setting",
      },
    });
    revisionsHandler = async () => ({ current: null, revisions: [] });
    const { host } = await render(page());
    await waitFor(() => computePicker(host));
    expect(host.querySelector(".recap-compute-rule")).toBeNull();
    expect(host.textContent).not.toContain("Dataset label rule");
    const rule = host.querySelector(".recap-label-rule")!.textContent!;
    expect(rule).toContain("Label rule (decided automatically)");
    expect(rule).toContain("Policy rollouts");
    expect(rule).toContain("fell back to policy rollouts");
    await click(button(host, "Compute advantages"));
    expect(run).toHaveBeenCalledTimes(1);
    expect(run.mock.calls[0][1]).toEqual({ checkpoint: "value-r1-step3000" });
    expect(host.querySelector('[role="progressbar"]')).not.toBeNull();
    expect(host.textContent).toContain("queued");
  });

  test("a dataset set to demonstrations or to values only says what the run will produce", async () => {
    statusHandler = async () => ({
      ...status(),
      dataset_type: {
        setting: "sft",
        dataset_type: "sft",
        source: "user",
        reason: "the dataset setting",
      },
    });
    const { host } = await render(page());
    await loaded(host);
    await click(button(host, "Recompute"));
    let rule = host.querySelector(".recap-label-rule")!.textContent!;
    expect(rule).toContain("Demonstrations (SFT)");
    expect(rule).toContain("set for this dataset");
    expect(rule).toContain("every Boolean label is set to positive");
    await click(button(host, "Recompute"));
    expect(run.mock.calls[0][1].dataset_type).toBeUndefined();

    statusHandler = async () => ({
      ...status(),
      dataset_type: {
        setting: "value_only",
        dataset_type: "value_only",
        source: "user",
        reason: "the dataset setting",
      },
    });
    const second = await render(page());
    await loaded(second.host);
    await click(button(second.host, "Recompute"));
    rule = second.host.querySelector(".recap-label-rule")!.textContent!;
    expect(rule).toContain("Values only (no success/failure labels)");
    expect(rule).toContain("Only the Value curve is computed");
  });

  test("read-only browsing retains history and comparison while hiding compute actions", async () => {
    context = { ...context, readOnly: true };
    const { host } = await render(page());
    await loaded(host);
    expect(button(host, "Recompute")).toBeUndefined();
    await choose(selectors(host)[1], r1);
    await waitFor(() => host.querySelector(".recap-comparison"));
    expect(run).not.toHaveBeenCalled();
    expect(
      host.querySelector(
        '[aria-label="Value-model checkpoint for computation"]',
      ),
    ).toBeNull();
  });

  test("locale switches translate controls, metrics and warnings without refetching result rows", async () => {
    const { host } = await render(page());
    await loaded(host);
    await choose(selectors(host)[1], r1);
    await waitFor(() => host.querySelector(".recap-comparison"));
    const reads = fetchEpisode.mock.calls.length;
    await click(host.querySelector("[data-language=zh]"));
    await flush();
    expect(selectors(host)[0].labels?.[0]?.textContent).toBe("查看历史结果 A");
    expect(host.textContent).toContain("数据集对比");
    expect(host.textContent).toContain("回报范围不同");
    expect(host.textContent).toContain("标签一致率");
    expect(host.textContent).not.toContain("Return ranges differ");
    expect(fetchEpisode.mock.calls.length).toBe(reads);
  });
});

const summaryWithRange = (rid: string, lo: number, hi: number): RecapSummary =>
  ({
    revision_id: rid,
    threshold: 0,
    episodes: {
      "0": {
        positive_fraction: 0.5,
        mean_advantage: 0,
        mean_value: (lo + hi) / 2,
        frames: 3,
        min_value: lo,
        max_value: hi,
      },
    },
  }) as unknown as RecapSummary;
const axisLabels = (host: HTMLElement) =>
  [
    ...host.querySelectorAll(
      ".recap-value-row:not(.recap-diff-row) .recap-axis",
    ),
  ]
    .map((node) => node.textContent)
    .join(" ");
const radios = (host: HTMLElement) => [
  ...host.querySelectorAll<HTMLButtonElement>(
    '[role="radiogroup"][aria-label="Value axis"] [role="radio"]',
  ),
];
const radio = (host: HTMLElement, text: string) =>
  radios(host).find((node) => node.textContent === text)!;

describe("Value axis", () => {
  test("defaults to an adaptive axis that zooms in on the curve, with numeric ticks", async () => {
    const { host } = await render(page());
    await loaded(host);
    expect(radio(host, "Adaptive").getAttribute("aria-checked")).toBe("true");
    // r2 runs -0.4…-0.2, so the labels are around there, not -1…0
    const labels = axisLabels(host);
    expect(labels).toContain("−0.4");
    expect(labels).toContain("−0.2");
    expect(labels).not.toContain("−1");
    const hint = host.querySelector(".recap-frame-readouts")!.textContent!;
    expect(hint).toContain("Axis");
    expect(hint).toContain("Adaptive");
    expect(hint).not.toContain("Normalized to");
    // the curve still draws and the playhead dot sits inside the plot
    expect(host.querySelector(".recap-line")?.getAttribute("points")).toMatch(
      /^[\d.,\- ]+$/,
    );
    const dot = host.querySelector<HTMLElement>(".recap-dot.side-a")!;
    const top = parseFloat(dot.style.top);
    expect(top).toBeGreaterThanOrEqual(0);
    expect(top).toBeLessThanOrEqual(100);
    // the zero line is outside this window, so no reference line is drawn
    expect(host.querySelector(".recap-ref")).toBeNull();
  });

  test("the fixed mode restores the −1…0 axis with its reference lines, and is remembered", async () => {
    const { host } = await render(page());
    await loaded(host);
    await click(radio(host, "Fixed −1…0"));
    expect(radio(host, "Fixed −1…0").getAttribute("aria-checked")).toBe("true");
    expect(axisLabels(host)).toContain("−1.0");
    expect(axisLabels(host)).toContain("0");
    expect(host.querySelectorAll(".recap-ref").length).toBe(2);
    expect(window.localStorage.getItem("levi.recap.valueAxis")).toBe("fixed");
    const again = await render(page());
    await loaded(again.host);
    expect(radio(again.host, "Fixed −1…0").getAttribute("aria-checked")).toBe(
      "true",
    );
  });

  test("a blocked or throwing storage still renders and changes the axis", async () => {
    const original = Object.getOwnPropertyDescriptor(window, "localStorage");
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      get() {
        throw new Error("blocked");
      },
    });
    try {
      const { host } = await render(page());
      await loaded(host);
      await click(radio(host, "Fixed −1…0"));
      expect(radio(host, "Fixed −1…0").getAttribute("aria-checked")).toBe(
        "true",
      );
    } finally {
      if (original) Object.defineProperty(window, "localStorage", original);
    }
  });

  test("dataset range and return units are disabled with a reason until their data exists", async () => {
    const { host } = await render(page());
    await loaded(host);
    expect(radio(host, "Dataset range").disabled).toBe(true);
    // the reason is visible text, and the control group points at it
    const note = host.querySelector<HTMLElement>(".recap-axis-note")!;
    expect(note.textContent).toContain("per-episode minima and maxima");
    expect(note.id).not.toBe("");
    expect(
      host
        .querySelector('[role="radiogroup"][aria-label="Value axis"]')!
        .getAttribute("aria-describedby"),
    ).toBe(note.id);
    // revisions carry return ranges, so return units are available
    expect(radio(host, "Return units").disabled).toBe(false);
  });

  test("dataset range is enabled by a stored spread and keeps the axis still", async () => {
    summaryHandler = async (_ident, rid) =>
      summaryWithRange(rid ?? r2, -0.9, -0.1);
    const { host } = await render(page());
    await loaded(host);
    await waitFor(() => !radio(host, "Dataset range").disabled);
    await click(radio(host, "Dataset range"));
    expect(radio(host, "Dataset range").getAttribute("aria-checked")).toBe(
      "true",
    );
    const labels = axisLabels(host);
    expect(labels).toContain("−0.8");
    expect(labels).toContain("−0.2");
  });

  test("while the summary loads or fails the reason says so, not 'not stored'", async () => {
    const pending = deferred<RecapSummary | null>();
    summaryHandler = () => pending.promise;
    const { host } = await render(page());
    await loaded(host);
    expect(host.querySelector(".recap-axis-note")?.textContent).toContain(
      "Loading the dataset range…",
    );
    await act(async () => pending.resolve(null));
    await waitFor(() =>
      host
        .querySelector(".recap-axis-note")
        ?.textContent?.includes("per-episode minima"),
    );
    summaryHandler = async () => {
      throw new Error("summary down");
    };
    await click(button(host, "Refresh results"));
    await waitFor(() =>
      host
        .querySelector(".recap-axis-note")
        ?.textContent?.includes("could not be read"),
    );
    expect(host.querySelector(".recap-axis-note")?.textContent).not.toContain(
      "does not store",
    );
  });

  test("with every mode available there is no reason text and no dangling description", async () => {
    summaryHandler = async (_ident, rid) =>
      summaryWithRange(rid ?? r2, -0.9, -0.1);
    const { host } = await render(page());
    await loaded(host);
    await waitFor(() => !radio(host, "Dataset range").disabled);
    expect(host.querySelector(".recap-axis-note")).toBeNull();
    expect(
      host
        .querySelector('[role="radiogroup"][aria-label="Value axis"]')!
        .hasAttribute("aria-describedby"),
    ).toBe(false);
  });

  test("a remembered mode that the data cannot serve falls back with an explanation", async () => {
    window.localStorage.setItem("levi.recap.valueAxis", "dataset");
    const { host } = await render(page());
    await loaded(host);
    expect(radio(host, "Adaptive").getAttribute("aria-checked")).toBe("true");
    const note = host.querySelector(".recap-axis-note")!;
    expect(note.textContent).toContain("Dataset range");
    expect(note.textContent).toContain("Showing the adaptive axis instead.");
  });

  test("return units show the original return scale", async () => {
    const { host } = await render(page());
    await loaded(host);
    await click(radio(host, "Return units"));
    // r2 return range is -799…0: V in -0.4…-0.2 is about -480…-640+799
    expect(axisLabels(host)).toMatch(/−?\d{2,3}/);
    expect(host.querySelector(".recap-frame-readouts")!.textContent).toContain(
      "original return",
    );
  });

  test("return units label every reading with its unit, A, B and the difference alike", async () => {
    const { host } = await render(page());
    await loaded(host);
    await choose(selectors(host)[1], r1);
    await waitFor(() => host.querySelector(".recap-diff-line"));
    await click(radio(host, "Return units"));
    // first frame: r2 V=-0.4 on -799…0 is -319.6; r1 V=-0.8 on -1000…0 is -800
    const a = host.querySelector(".recap-frame-readout.side-a")!.textContent!;
    const b = host.querySelector(".recap-frame-readout.side-b")!.textContent!;
    const d = host.querySelector(
      ".recap-frame-readout.side-diff",
    )!.textContent!;
    expect(a).toContain("original return -319.6");
    expect(b).toContain("original return -800.0");
    expect(d).toContain("Δ -480.40 original return");
    // in normalized units the readings carry no return label
    await click(radio(host, "Adaptive"));
    expect(
      host.querySelector(".recap-frame-readout.side-a")!.textContent,
    ).not.toContain("original return");
    expect(
      host.querySelector(".recap-frame-readout.side-diff")!.textContent,
    ).not.toContain("original return");
  });

  test("a constant curve draws a flat line mid-plot, not a broken one", async () => {
    episodeHandler = async (_ep, _ident, _signal, rid) =>
      episode(rid!, 0, -0.5);
    const { host } = await render(page());
    await loaded(host);
    const points = host.querySelector(".recap-line")!.getAttribute("points")!;
    expect(points).not.toContain("NaN");
    const ys = points.split(" ").map((p) => parseFloat(p.split(",")[1]));
    expect(new Set(ys).size).toBe(1);
    expect(ys[0]).toBeCloseTo(50, 0);
  });

  test("comparison keeps one shared axis and adds a B − A curve with a zero line", async () => {
    const { host } = await render(page());
    await loaded(host);
    await choose(selectors(host)[1], r1);
    await waitFor(() => host.querySelector(".recap-diff-line"));
    expect(host.querySelectorAll(".recap-line")).toHaveLength(2);
    expect(host.querySelectorAll(".recap-diff-line")).toHaveLength(1);
    expect(host.querySelector(".recap-diff-row .recap-ref")).not.toBeNull();
    // both curves (-0.8…-0.2) fit in the one axis
    expect(axisLabels(host)).toContain("−0.8");
    const diff = host.querySelector(".recap-frame-readout.side-diff")!;
    // r1 minus r2 at the first frame: -0.8 - (-0.4)
    expect(diff.textContent).toContain("Δ -0.400");
    // single-result view has no difference row
    await choose(selectors(host)[1], "");
    expect(host.querySelector(".recap-diff-line")).toBeNull();
  });

  test("the rows expand and collapse from the keyboard-reachable button", async () => {
    const { host } = await render(page());
    await loaded(host);
    const row = host.querySelector(".recap-value-row")!;
    expect(row.classList.contains("expanded")).toBe(false);
    const toggle = button(host, "Expand value rows");
    expect(toggle.getAttribute("aria-pressed")).toBe("false");
    await click(toggle);
    expect(row.classList.contains("expanded")).toBe(true);
    expect(
      button(host, "Collapse value rows").getAttribute("aria-pressed"),
    ).toBe("true");
  });

  test("the axis controls are translated", async () => {
    const { host } = await render(page());
    await loaded(host);
    await click(host.querySelector("[data-language=zh]"));
    await flush();
    expect(
      host.querySelector('[role="radiogroup"][aria-label="Value 纵轴"]'),
    ).not.toBeNull();
    expect(host.textContent).toContain("自适应");
    expect(host.textContent).toContain("回报单位");
    expect(host.textContent).toContain("展开 Value 行");
  });
});

// "models" layout: one result per value model, keyed by the model's name;
// recomputing keeps the key and changes the version.
const modelRow = (
  model: string,
  version: string,
  more: Partial<RecapRevision> = {},
): RecapRevision =>
  revision(r2, {
    revision_id: model,
    model,
    checkpoint: model,
    version,
    layout: "models",
    ...more,
  });

describe("one result per value model", () => {
  test("per-run results of a model are folded behind a checkbox; a model result hides them", async () => {
    const older = "20261001-090000";
    revisionsHandler = async () => ({
      current: "value-r2-step6000",
      revisions: [
        modelRow("value-r2-step6000", "20261010-100000", {
          created_at: 1790000300,
          current: true,
        }),
        revision(r1, { created_at: 1790000200, current: false }),
        revision(older, {
          checkpoint: "value-r1-step3000",
          created_at: 1790000100,
          current: false,
        }),
        revision("20260930-090000", {
          checkpoint: "value-r2-step6000",
          created_at: 1790000000,
          current: false,
        }),
      ],
    });
    statusHandler = async () => ({
      ...status(),
      current: modelRow("value-r2-step6000", "20261010-100000"),
    });
    const { host } = await render(page());
    await waitFor(() => selectors(host)[0]?.value === "value-r2-step6000", {
      label: "model result selected",
    });
    const ids = () => [...selectors(host)[0].options].map((o) => o.value);
    // One row per model: the model result and r1's newest run.
    expect(ids()).toEqual(["value-r2-step6000", r1]);
    expect(selectors(host)[0].options[1].textContent).toContain(
      "per-run result",
    );
    const box = host.querySelector<HTMLInputElement>(".recap-earlier input")!;
    expect(host.querySelector(".recap-earlier")!.textContent).toContain(
      "Show earlier per-run results (2)",
    );
    await click(box);
    expect(ids()).toEqual(["value-r2-step6000", r1, older, "20260930-090000"]);
    // A selected earlier run stays listed when the checkbox is cleared.
    await choose(selectors(host)[1], older);
    await click(box);
    expect([...selectors(host)[1].options].map((o) => o.value)).toContain(
      older,
    );
    expect(host.textContent).toContain(
      "One result per value model: recomputing with the same model replaces its result.",
    );
  });

  test("recomputing replaces the model's version and keeps both A/B selections", async () => {
    let version = "20261010-100000";
    revisionsHandler = async () => ({
      current: "value-r2-step6000",
      revisions: [
        modelRow("value-r2-step6000", version, { current: true }),
        modelRow("value-r1-step3000", "20261009-100000", {
          checkpoint: "value-r1-step3000",
          current: false,
        }),
      ],
    });
    statusHandler = async () => ({
      ...status(),
      current: modelRow("value-r2-step6000", version),
      job: version.endsWith("100000")
        ? null
        : { ...job, status: "succeeded", revision_id: "value-r2-step6000" },
    });
    episodeHandler = async (ep, _ident, _signal, rid) =>
      episode(rid === "value-r1-step3000" ? r1 : r2, ep);
    const { host } = await render(page());
    await waitFor(() => selectors(host)[0]?.value === "value-r2-step6000");
    await choose(selectors(host)[1], "value-r1-step3000");
    await waitFor(() => host.querySelector(".recap-comparison"));
    const pinned = fetchEpisode.mock.calls.filter(
      (call) => call[3] === "value-r2-step6000",
    );
    expect(pinned.at(-1)?.[4]).toBe("20261010-100000");
    expect(fetchCompare.mock.calls.at(-1)?.[4]).toEqual({
      a: "20261010-100000",
      b: "20261009-100000",
    });
    // Someone recomputes r2 with the same model; the list is read again.
    version = "20261010-110000";
    await click(button(host, "Refresh results"));
    await waitFor(
      () =>
        fetchEpisode.mock.calls.some((call) => call[4] === "20261010-110000"),
      { label: "new version read" },
    );
    expect(selectors(host)[0].value).toBe("value-r2-step6000");
    expect(selectors(host)[1].value).toBe("value-r1-step3000");
    expect(fetchCompare.mock.calls.at(-1)?.[4]).toEqual({
      a: "20261010-110000",
      b: "20261009-100000",
    });
  });

  test("a read answered 'recomputed' reloads the list and says so, without an error", async () => {
    let version = "20261010-100000";
    let lists = 0;
    revisionsHandler = async () => {
      lists += 1;
      return {
        current: "value-r2-step6000",
        revisions: [modelRow("value-r2-step6000", version, { current: true })],
      };
    };
    statusHandler = async () => ({
      ...status(),
      current: modelRow("value-r2-step6000", version),
    });
    episodeHandler = async (ep, _ident, _signal, _rid, pinned) => {
      if (pinned === "20261010-100000") {
        // Recomputed by someone else after the list was read.
        version = "20261010-110000";
        throw new RecapRecomputedError("recomputed; reload it");
      }
      return episode(r2, ep);
    };
    const { host } = await render(page());
    await waitFor(() => host.querySelector(".recap-line"), {
      label: "new version drawn",
    });
    expect(lists).toBe(2);
    expect(host.textContent).toContain(
      "A selected result was recomputed; the newest version is shown.",
    );
    expect(host.querySelector('[role="alert"]')).toBeNull();
    expect(fetchEpisode.mock.calls.at(-1)?.[4]).toBe("20261010-110000");
  });

  test("a list that keeps naming the old version does not loop", async () => {
    let lists = 0;
    revisionsHandler = async () => {
      lists += 1;
      return {
        current: "value-r2-step6000",
        revisions: [
          modelRow("value-r2-step6000", "20261010-100000", { current: true }),
        ],
      };
    };
    statusHandler = async () => ({
      ...status(),
      current: modelRow("value-r2-step6000", "20261010-100000"),
    });
    episodeHandler = async () => {
      throw new RecapRecomputedError("recomputed");
    };
    const { host } = await render(page());
    await waitFor(() =>
      host.textContent?.includes("A selected result was recomputed"),
    );
    await flush();
    await flush();
    expect(lists).toBeLessThanOrEqual(2);
  });
});

const valuesOnlyEpisode = (ep = 0): RecapEpisode => ({
  ...episode(r2, ep),
  threshold: null,
  labels: false,
  advantage: [],
  positive: [],
});

describe("values-only results", () => {
  test("never show advantage or positive/negative wording", async () => {
    const row = revision(r2, {
      dataset_type: "value_only",
      labels: false,
      threshold: null as unknown as number,
      positive_fraction: null,
    });
    revisionsHandler = async () => ({ current: r2, revisions: [row] });
    statusHandler = async () => ({ ...status(), current: row });
    episodeHandler = async (ep) => valuesOnlyEpisode(ep);
    const { host } = await render(page());
    await waitFor(() => host.querySelector(".recap-line"));
    const readout = host.querySelector(".recap-frame-readout.side-a")!;
    expect(readout.textContent).toContain("values only");
    expect(readout.textContent).not.toContain("negative");
    expect(readout.textContent).not.toContain("positive");
    expect(readout.textContent).not.toContain(" A ");
    const dot = host.querySelector(".recap-dot.side-a")!;
    expect(dot.classList.contains("values-only")).toBe(true);
    expect(dot.classList.contains("neg")).toBe(false);
    expect(host.querySelectorAll(".tl-seg.adv")).toHaveLength(0);
    expect(
      host.querySelector(".recap-advantage-row .recap-track-state")!
        .textContent,
    ).toContain("Values only (no success/failure labels)");
    expect(host.querySelector(".recap-meta")!.textContent).toContain(
      "Values only (no success/failure labels)",
    );
    expect(host.querySelector(".recap-legend")).toBeNull();
    const facts = host.querySelector(".recap-version-facts")!.textContent!;
    expect(facts).toContain("Values only: no outcome labels were used");
    expect(facts).not.toContain("positive frames");
    expect(selectors(host)[0].options[0].textContent).toContain("values only");
  });
});

describe("comparison charts", () => {
  const charted = (a: string, b: string): RecapComparison => ({
    ...compare(a, b),
    by_outcome: [
      {
        outcome: "success",
        episodes: 1,
        frames: 3,
        mean_value_a: -0.3,
        mean_value_b: -0.6,
        label_agreement: 0,
        positive_fraction_a: 2 / 3,
        positive_fraction_b: 1 / 3,
      },
    ],
    distribution: {
      bins: 2,
      value: { edges: [-0.8, -0.5, -0.2], a: [1, 2], b: [2, 1] },
      abs_diff: { edges: [0, 0.2, 0.4], counts: [1, 2] },
    },
  });

  test("draws the agreement, outcome, distribution and per-episode charts with tables", async () => {
    compareHandler = async (_ident, a, b) => charted(a, b);
    const { host } = await render(page());
    await loaded(host);
    await choose(selectors(host)[1], r1);
    await waitFor(() => host.querySelector(".recap-charts"));
    const captions = [...host.querySelectorAll(".recap-chart figcaption")].map(
      (node) => node.textContent,
    );
    expect(captions).toEqual([
      "Label agreement and positive frames by outcome",
      "Mean Value by outcome",
      "Value distribution · per shared frame",
      "Per-frame |B − A| distribution",
      "Per-episode mean Value difference (B − A)",
    ]);
    // Recharts draws real bars at the fallback width in a test DOM.
    expect(
      host.querySelectorAll(".recap-chart .recharts-bar-rectangle").length,
    ).toBeGreaterThan(5);
    // Charts are hidden from assistive technology; text and tables carry
    // the numbers, and A/B are named in text.
    for (const plot of host.querySelectorAll(".recap-chart-plot"))
      expect(plot.getAttribute("aria-hidden")).toBe("true");
    const first = host.querySelector(".recap-chart")!;
    expect(first.querySelector(".recap-chart-summary")!.textContent).toContain(
      "Success episodes: Label agreement 0.0%",
    );
    expect(first.textContent).toContain("A · value-r2-step6000");
    expect(first.textContent).toContain("B · value-r1-step3000");
    const table = first.querySelector(".recap-chart-table")!;
    expect(table.querySelector("summary")!.textContent).toBe("Table view");
    expect(table.querySelector("table")!.textContent).toContain("66.7%");
    const histogram = host.querySelectorAll(".recap-chart")[2];
    expect(histogram.querySelector("tbody")!.textContent).toContain(
      "-0.800…-0.500",
    );
    const perEpisode = host.querySelectorAll(".recap-chart")[4];
    expect(perEpisode.querySelector("tbody")!.textContent).toContain(
      "0 · current",
    );
    expect(
      perEpisode.querySelector(".recharts-bar-rectangle .current"),
    ).not.toBeNull();
    // Bars carry no written colour: their classes take the design tokens.
    expect(
      host.querySelector('.recap-chart .recharts-bar-rectangle [fill^="#"]'),
    ).toBeNull();
  });

  test("an older backend without aggregates still gets charts from per-episode rows", async () => {
    const { host } = await render(page());
    await loaded(host);
    await choose(selectors(host)[1], r1);
    await waitFor(() => host.querySelector(".recap-charts"));
    const captions = [...host.querySelectorAll(".recap-chart figcaption")].map(
      (node) => node.textContent,
    );
    expect(captions).toContain("Value distribution · per episode (mean Value)");
    expect(captions).not.toContain("Per-frame |B − A| distribution");
  });

  test("chart text is translated", async () => {
    compareHandler = async (_ident, a, b) => charted(a, b);
    const { host } = await render(page());
    await loaded(host);
    await choose(selectors(host)[1], r1);
    await waitFor(() => host.querySelector(".recap-charts"));
    await click(host.querySelector("[data-language=zh]"));
    await flush();
    expect(host.textContent).toContain("按结局分组的平均价值");
    expect(host.textContent).toContain("表格视图");
    expect(host.textContent).not.toContain("Mean Value by outcome");
  });
});
