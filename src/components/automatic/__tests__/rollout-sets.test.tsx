import { fire, render, setupDom, waitFor } from "@/components/ds/__tests__/dom";
import { afterEach, describe, expect, mock, test } from "bun:test";
import { mockFetch } from "./fixtures";
import { LocaleProvider } from "@/components/levi-locale";
import type { RolloutAttempt, RolloutsResponse } from "../types";

mock.module("next/link", () => ({
  default: ({ children, ...props }: Record<string, unknown>) => (
    <a {...(props as object)}>{children as never}</a>
  ),
}));
const { RolloutSets, RolloutSetsSection } = await import("../rollout-sets");
setupDom();

let restore = () => {};
afterEach(() => restore());

const RUN = "aeri-20261018-ext-r08";
const URL_SIDE =
  "/api/levi/files/live.g__f__subtask/videos/chunk-000/observation.images.side/episode_000000.mp4";
const URL_WRIST =
  "/api/levi/files/live.g__f__subtask/videos/chunk-000/observation.images.wrist/episode_000000.mp4";

function attempt(over: Partial<RolloutAttempt> = {}): RolloutAttempt {
  return {
    attempt: 1,
    episode_id: `${RUN}.forward.0001`,
    demo: "g/f__subtask/demo_0001",
    instruction: "put the red pepper into the white plate",
    frames: 200,
    seconds: 26.5,
    started_at: "2026-10-18T11:02:25Z",
    ended_at: "2026-10-18T11:02:52Z",
    stop_reason: "subtask_ended",
    check: { value: true },
    sealed: true,
    media: {
      available: true,
      dataset: "local/live.g__f__subtask",
      episode_index: 0,
      viewer: "/local/live.g__f__subtask/episode_0",
      videos: [
        { camera: "observation.images.side", url: URL_SIDE },
        { camera: "observation.images.wrist", url: URL_WRIST },
      ],
    },
    ...over,
  };
}

/** Two rounds: round 1 with a forward set and a reverse set that needed a
 * retry (the second attempt was never sealed: no demo, no video), labelled
 * by the operator; round 2 not labelled yet, so the server withheld its
 * forward check, stop reason and outcomes (the blind rule) while its reverse
 * attempt stays visible. One problem entry. */
function fixture(): RolloutsResponse {
  return {
    schema: "levi.aeri.rollouts.v1",
    run_id: RUN,
    task: "pick the pepper into the white plate",
    mode: "external",
    reset_mode: "subtask_loop",
    generated_from: { journal_sha256: "c".repeat(64), events: 123 },
    rollouts: [
      {
        round: 1,
        round_id: "r1",
        released_at: "2026-10-18T11:02:00Z",
        outcome: "forward_done",
        operator_label: { value: "success", by: "op", source: "terminal" },
        notes: [],
        sets: {
          forward: {
            plan_file: "subtasks/000010-plan.json",
            task: "pick the pepper into the white plate",
            outcome: "done",
            subtasks: [
              {
                index: 0,
                instruction: "put the red pepper into the white plate",
                attempts: [attempt()],
              },
            ],
          },
          reverse: {
            plan_file: "subtasks/000020-plan.json",
            task: "undo: pick the pepper into the white plate",
            outcome: "done",
            subtasks: [
              {
                index: 0,
                instruction: "put the red pepper into the blue plate",
                attempts: [
                  attempt({
                    episode_id: `${RUN}.reset.0001`,
                    demo: "g/f__reverse/demo_0001",
                    instruction: "put the red pepper into the blue plate",
                    stop_reason: "horizon_exhausted",
                    check: { value: false },
                    media: { available: false, reason: "not_in_view" },
                  }),
                  attempt({
                    attempt: 2,
                    episode_id: `${RUN}.reset.0002`,
                    demo: null,
                    instruction: "put the red pepper into the blue plate",
                    frames: 87,
                    stop_reason: "safety_stop",
                    check: { value: null },
                    sealed: false,
                    incomplete: {
                      folder: "g/f__reverse/incomplete_demo_0002",
                      why: "recording not sealed (incomplete_*)",
                    },
                    operator_label: "discard",
                    media: { available: false, reason: "no_demo" },
                  }),
                ],
              },
            ],
          },
        },
      },
      {
        round: 2,
        round_id: "r2",
        released_at: "2026-10-18T11:10:00Z",
        outcome: "hidden_until_labelled",
        notes: [
          {
            code: "motion_refused",
            detail: "home pose not reached",
            at: "2026-10-18T11:11:00Z",
          },
        ],
        sets: {
          forward: {
            plan_file: null,
            task: null,
            outcome: "hidden_until_labelled",
            subtasks: [
              {
                index: 0,
                instruction: "put the red pepper into the white plate",
                attempts: [
                  attempt({
                    episode_id: `${RUN}.forward.0002`,
                    demo: "g/f__subtask/demo_0002",
                    stop_reason: "hidden_until_labelled",
                    check: { hidden_until_labelled: true },
                    media: { available: false, reason: "no_dataset" },
                  }),
                ],
              },
            ],
          },
          reverse: {
            plan_file: null,
            task: null,
            outcome: "done",
            subtasks: [
              {
                index: 0,
                instruction: "put the red pepper into the blue plate",
                attempts: [
                  attempt({
                    episode_id: `${RUN}.reset.0003`,
                    demo: "g/f__reverse/demo_0003",
                    stop_reason: "horizon_exhausted",
                    check: { value: false },
                    media: { available: false, reason: "no_dataset" },
                  }),
                ],
              },
            ],
          },
        },
      },
    ],
    unmatched_demos: [],
    problems: [
      {
        kind: "attempt_without_demo",
        episode_id: `${RUN}.reset.0004`,
      },
    ],
  };
}

const rounds = (host: ParentNode) =>
  Array.from(host.querySelectorAll('[data-testid="rollout-round"]'));
const attempts = (node: ParentNode) =>
  Array.from(node.querySelectorAll('[data-testid="rollout-attempt"]'));
const row = (node: ParentNode, name: string) =>
  node.querySelector(`[data-row="${name}"]`)?.textContent ?? "";

async function withLanguage<T>(language: string, body: () => Promise<T>) {
  const previous = window.localStorage.getItem("levi-language");
  window.localStorage.setItem("levi-language", language);
  try {
    return await body();
  } finally {
    if (previous === null) window.localStorage.removeItem("levi-language");
    else window.localStorage.setItem("levi-language", previous);
  }
}

describe("the rollout sets view", () => {
  test("rounds, forward / reverse sets, subtasks and every attempt", async () => {
    const { host } = await render(<RolloutSets data={fixture()} />);
    const [first, second] = rounds(host);
    expect(rounds(host)).toHaveLength(2);
    expect(first.textContent).toContain("Round 1");
    expect(first.textContent).toContain("Forward done (automatic)");
    // Round 1: forward set with one attempt, reverse set with a retry.
    const forward = first.querySelector('[data-testid="rollout-set-forward"]')!;
    const reverse = first.querySelector('[data-testid="rollout-set-reverse"]')!;
    expect(forward.textContent).toContain("Forward set");
    expect(reverse.textContent).toContain("Reverse set");
    expect(attempts(forward)).toHaveLength(1);
    expect(attempts(reverse)).toHaveLength(2);
    expect(reverse.textContent).toContain("2 attempts");
    const [one, two] = attempts(reverse);
    expect(one.textContent).toContain(`${RUN}.reset.0001`);
    expect(row(one, "frames")).toBe("200");
    expect(row(one, "seconds")).toContain("26.5 s");
    expect(row(one, "stop")).toBe("Step limit reached");
    // The check is named a model judgement, never a success.
    expect(row(one, "check")).toBe("Not done (model judgement)");
    expect(row(attempts(forward)[0], "check")).toBe("Done (model judgement)");
    expect(host.textContent).not.toContain("Success");
    expect(host.textContent).toContain("not a label");
    // The unsealed retry: no demo, the reason, the operator's label.
    expect(row(two, "demo")).toBe("Recording not sealed");
    expect(two.textContent).toContain("recording not sealed (incomplete_*)");
    expect(row(two, "check")).toBe("The model could not tell");
    expect(row(two, "label")).toBe("discard");
    expect(row(one, "label")).toBe("None");
    // Missing videos say why, and nothing is played.
    expect(
      one.querySelector('[data-testid="rollout-video-missing"]')?.textContent,
    ).toContain("Video not available");
    expect(
      two.querySelector('[data-testid="rollout-video-missing"]')?.textContent,
    ).toContain("no demo for this attempt");
    // Round 1 is labelled: its forward judgement and outcome show.
    expect(first.textContent).toContain("Operator's round label: success");
    // Round 2 is not: forward check, stop reason and outcomes wait for the
    // label; the reverse attempt and the round's notes stay visible.
    const fwd2 = second.querySelector('[data-testid="rollout-set-forward"]')!;
    const rev2 = second.querySelector('[data-testid="rollout-set-reverse"]')!;
    const hiddenOne = attempts(fwd2)[0];
    expect(row(hiddenOne, "check")).toBe("Shown after labelling");
    expect(row(hiddenOne, "stop")).toBe("Shown after labelling");
    expect(second.querySelector(".ar-subtask__head")?.textContent).toContain(
      "Shown after labelling",
    );
    expect(fwd2.querySelector(".ar-subtask__head")?.textContent).toContain(
      "Shown after labelling",
    );
    expect(second.textContent).not.toContain("Forward done");
    expect(row(attempts(rev2)[0], "check")).toBe("Not done (model judgement)");
    expect(row(attempts(rev2)[0], "stop")).toBe("Step limit reached");
    expect(
      second.querySelector('[data-testid="rollout-notes"]')?.textContent,
    ).toContain("motion_refused");
    // The problem entry is listed.
    expect(
      host.querySelector('[data-testid="rollouts-problems"]')?.textContent,
    ).toContain("attempt_without_demo");
    // Read-only: no button at all (disclosures are <summary>).
    expect(host.querySelectorAll("button")).toHaveLength(0);
    expect(host.querySelectorAll("video")).toHaveLength(0);
  });

  test("the pending card's episode keeps its judgement withheld", async () => {
    const { host } = await render(
      <RolloutSets
        data={fixture()}
        hiddenEpisodes={new Set([`${RUN}.forward.0001`])}
      />,
    );
    const first = attempts(rounds(host)[0])[0];
    expect(row(first, "check")).toBe("Shown after labelling");
    expect(row(first, "stop")).toBe("Shown after labelling");
  });

  test("videos mount only when opened, from the file service", async () => {
    const original = globalThis.fetch;
    globalThis.fetch = mock(() =>
      Promise.reject(new Error("offline")),
    ) as unknown as typeof fetch;
    try {
      const { host } = await render(<RolloutSets data={fixture()} />);
      const videos = host.querySelector(
        '[data-testid="rollout-videos"]',
      ) as HTMLDetailsElement;
      expect(videos.textContent).toContain("Show the camera videos (2)");
      expect(videos.querySelector("a")?.getAttribute("href")).toBe(
        "/local/live.g__f__subtask/episode_0",
      );
      expect(videos.querySelectorAll("video")).toHaveLength(0);
      videos.open = true;
      await fire(videos, new Event("toggle"));
      await waitFor(() => videos.querySelectorAll("video").length === 2);
      const sources = Array.from(videos.querySelectorAll("source")).map((s) =>
        s.getAttribute("src"),
      );
      expect(sources).toEqual([URL_SIDE, URL_WRIST]);
    } finally {
      globalThis.fetch = original;
    }
  });

  test("Chinese: the same view in the other catalogue", async () => {
    await withLanguage("zh", async () => {
      const { host } = await render(
        <LocaleProvider>
          <RolloutSets data={fixture()} />
        </LocaleProvider>,
      );
      await waitFor(() => host.textContent?.includes("第 1 轮"));
      const [first, second] = rounds(host);
      const reverse = first.querySelector(
        '[data-testid="rollout-set-reverse"]',
      )!;
      expect(first.textContent).toContain("正向集");
      expect(reverse.textContent).toContain("逆向集");
      expect(reverse.textContent).toContain("2 次尝试");
      const [one, two] = attempts(reverse);
      expect(row(one, "check")).toBe("未完成（模型判断）");
      expect(row(one, "stop")).toBe("达到步数上限");
      expect(row(two, "demo")).toBe("录制未封存");
      expect(one.textContent).toContain("视频不可用");
      const hidden = attempts(
        second.querySelector('[data-testid="rollout-set-forward"]')!,
      )[0];
      expect(row(hidden, "check")).toBe("打标签后显示");
      expect(row(hidden, "stop")).toBe("打标签后显示");
      expect(
        row(
          attempts(
            second.querySelector('[data-testid="rollout-set-reverse"]')!,
          )[0],
          "check",
        ),
      ).toBe("未完成（模型判断）");
      expect(host.textContent).toContain("不是标签");
      expect(host.textContent).toContain("匹配 demo 时发现的问题（1）");
    });
  });
});

describe("the rollout sets section", () => {
  test.each([
    ["en", "Rollouts", "Show the rollout sets"],
    ["zh", "Rollout 集", "显示 rollout 集"],
  ])(
    "%s: fetches the manifest only when opened, with GET alone",
    async (language, title, open) => {
      await withLanguage(language, async () => {
        const m = mockFetch({
          [`GET runs/${RUN}/rollouts`]: () => fixture(),
        });
        restore = m.restore;
        const { host } = await render(
          <LocaleProvider>
            <RolloutSetsSection runId={RUN} seq={41} />
          </LocaleProvider>,
        );
        await waitFor(() => host.textContent?.includes(title));
        expect(host.querySelector("summary")?.textContent).toBe(open);
        expect(m.calls).toHaveLength(0);
        const details = host.querySelector("details") as HTMLDetailsElement;
        details.open = true;
        await fire(details, new Event("toggle"));
        await waitFor(() => rounds(host).length === 2);
        expect(m.calls).toEqual([
          { method: "GET", path: `runs/${RUN}/rollouts`, body: undefined },
        ]);
      });
    },
  );

  test("a failed load is named", async () => {
    const m = mockFetch({
      [`GET runs/${RUN}/rollouts`]: () => ({
        __status: 503,
        body: { detail: { code: "rollouts_unavailable", message: "no" } },
      }),
    });
    restore = m.restore;
    const { host } = await render(<RolloutSetsSection runId={RUN} seq={1} />);
    const details = host.querySelector("details") as HTMLDetailsElement;
    details.open = true;
    await fire(details, new Event("toggle"));
    await waitFor(() =>
      host.textContent?.includes("The rollout sets could not be loaded"),
    );
  });
});
