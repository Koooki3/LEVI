import { render, setupDom, waitFor } from "@/components/ds/__tests__/dom";
import { afterEach, describe, expect, mock, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { mockFetch, requirements, snapshot } from "./fixtures";
import {
  FRAME,
  happy,
  refused,
  released,
  services,
  retry,
  reverse,
  unknown,
  wakeFailed,
} from "./subtask-fixtures";
import { LocaleProvider } from "@/components/levi-locale";
import type {
  RequestEvidence,
  SubtaskProvider,
  SubtasksResponse,
} from "../types";
import { RequirementsCard } from "../requirements-card";
import { SubtaskLoopView } from "../subtask-loop";

mock.module("next/link", () => ({
  default: ({ children, ...props }: Record<string, unknown>) => (
    <a {...(props as object)}>{children as never}</a>
  ),
}));
const { RunWindow } = await import("../run-window");
setupDom();

let restore = () => {};
afterEach(() => restore());

const subtaskRows = (host: ParentNode) =>
  Array.from(host.querySelectorAll("li.ar-subtask")).map((li) => ({
    index: li.getAttribute("data-index"),
    status: li.getAttribute("data-status"),
  }));
const row = (host: ParentNode, name: string) =>
  host.querySelector(`[data-testid="subtask-now"] [data-row="${name}"]`)
    ?.textContent ?? "";

describe("the subtask-loop view", () => {
  test("happy path: whitelist, plan, both subtasks done by the model", async () => {
    const { host } = await render(
      <SubtaskLoopView runId="run-1" data={happy()} />,
    );
    const inv = host.querySelector('[data-testid="subtask-inventory"]');
    expect(inv?.textContent).toContain("Confirmed: the frozen whitelist");
    expect(inv?.textContent).toContain("red cup");
    expect(inv?.textContent).toContain("confirmed by operator");
    expect(subtaskRows(host)).toEqual([
      { index: "0", status: "done" },
      { index: "1", status: "done" },
    ]);
    expect(host.textContent).toContain("put the red cup on the tray");
    expect(host.textContent).toContain("the red cup is on the tray");
    expect(host.textContent).toContain("Loop finished: 2 subtasks, 0 resends");
    // The model's verdict is named a judgement wherever it is shown.
    expect(host.textContent).toContain("Done (model judgement)");
    expect(host.textContent).toContain("not a label");
    expect(row(host, "round")).toBe("1");
    expect(row(host, "loop")).toBe("Forward loop");
    expect(row(host, "vlm")).toContain("Asleep");
    expect(row(host, "mem")).toBe("26.0 GiB");
    // The check's frames are evidence frames of the run.
    const img = host.querySelector("img");
    expect(img?.getAttribute("src")).toBe(
      `/api/levi/automatic/runs/run-1/frames/${FRAME}`,
    );
    // A frame not stored with the run is named, not shown as a broken image.
    expect(
      host.querySelector('[data-testid="subtask-frames-missing"]')?.textContent,
    ).toContain("front:check-2");
    expect(
      Array.from(host.querySelectorAll("img")).every((i) =>
        i.getAttribute("src")?.endsWith(FRAME),
      ),
    ).toBe(true);
  });

  test("retry: the resent subtask shows two attempts, the next one runs", async () => {
    const { host } = await render(
      <SubtaskLoopView runId="run-1" data={retry()} maxRetries={5} />,
    );
    expect(subtaskRows(host)).toEqual([
      { index: "0", status: "done" },
      { index: "1", status: "running" },
    ]);
    const first = host.querySelector('li.ar-subtask[data-index="0"]');
    expect(first?.textContent).toContain("Attempts: 2 (at most 5 resends)");
    expect(first?.textContent).toContain("Not done (model judgement)");
    const running = host.querySelector('li.ar-subtask[aria-current="step"]');
    expect(running?.getAttribute("data-index")).toBe("1");
    expect(row(host, "subtask")).toBe("Subtask 2");
  });

  test("the model could not tell: says the run waits for the operator", async () => {
    const { host } = await render(
      <SubtaskLoopView runId="run-1" data={unknown()} />,
    );
    expect(subtaskRows(host)[0].status).toBe("unknown");
    expect(
      host.querySelector('[data-testid="subtask-check"]')?.textContent,
    ).toBe("The model could not tell: waiting for the operator");
  });

  test("refused plan: the reason is shown, no subtask", async () => {
    const { host } = await render(
      <SubtaskLoopView runId="run-1" data={refused()} />,
    );
    expect(host.textContent).toContain("The model refused to plan this loop");
    expect(host.textContent).toContain("green cup");
    expect(subtaskRows(host)).toEqual([]);
  });

  test("wake failure: the state, the reason and the last MemAvailable", async () => {
    const { host } = await render(
      <SubtaskLoopView runId="run-1" data={wakeFailed()} />,
    );
    expect(row(host, "vlm")).toContain("Could not wake");
    expect(row(host, "vlm")).toContain("below the 8 GiB floor");
    expect(row(host, "mem")).toBe("9.5 GiB");
  });

  test("reverse loop: shown after the forward loop, its subtasks are reset episodes", async () => {
    const { host } = await render(
      <SubtaskLoopView runId="run-1" data={reverse()} />,
    );
    const loops = Array.from(host.querySelectorAll("[data-loop]")).map((n) =>
      n.getAttribute("data-loop"),
    );
    expect(loops).toEqual(["forward", "reverse"]);
    expect(row(host, "loop")).toBe("Reverse loop (puts the scene back)");
    const back = host.querySelector('[data-loop="reverse"]');
    expect(back?.textContent).toContain("put the red cup back on the table");
    expect(back?.textContent).toContain("run-1.reset.0001");
    expect(back?.textContent).toContain("(reset episode)");
  });

  test("a check of the episode the card hides is withheld, with its frames", async () => {
    const { host } = await render(
      <SubtaskLoopView
        runId="run-1"
        data={retry()}
        hiddenEpisodes={new Set(["run-1.forward.0002"])}
      />,
    );
    expect(subtaskRows(host)[0].status).toBe("hidden");
    expect(host.textContent).toContain(
      "Withheld until the episode is labelled",
    );
  });

  test("a released round and a refused wake are shown in words", async () => {
    const { host } = await render(
      <SubtaskLoopView runId="run-1" data={released()} />,
    );
    expect(
      host.querySelector('[data-testid="subtask-released"]')?.textContent,
    ).toBe("Forward loop released at the terminal");
    expect(row(host, "vlm")).toContain("Not woken (not enough GPU memory)");
    expect(row(host, "vlm")).toContain("not enough free GPU memory");
  });

  test("service steps, GPU sharing and the server's phase", async () => {
    const { host } = await render(
      <SubtaskLoopView
        runId="run-1"
        data={services()}
        progress={{ phase: "reverse_pending", rounds_ended: 1, round: 1 }}
      />,
    );
    expect(row(host, "alternation")).toBe(
      "The model stays loaded beside the policy · refused: not enough GPU headroom",
    );
    expect(row(host, "loop")).toBe(
      "Forward loop over; the reverse loop is next",
    );
    expect(row(host, "rounds_ended")).toBe("1");
    const steps = host.querySelector('[data-testid="subtask-services"]');
    expect(steps?.textContent).toContain("vlm_ready");
    expect(steps?.textContent).toContain("failed");
  });

  test("before any note it says what it waits for", async () => {
    const { host } = await render(
      <SubtaskLoopView runId="run-1" data={null} />,
    );
    expect(host.textContent).toContain("Nothing recorded for the subtask loop");
  });
});

describe("the page cannot send a subtask or round command", () => {
  test("the view has no control: no button, input, select or textarea", async () => {
    for (const data of [happy(), retry(), refused(), wakeFailed(), reverse()]) {
      const { host } = await render(
        <SubtaskLoopView runId="run-1" data={data} />,
      );
      const view = host.querySelector('[data-testid="subtask-loop"]');
      expect(
        view?.querySelectorAll("button, input, select, textarea, form").length,
      ).toBe(0);
    }
  });

  test("the API client has no write for a subtask or a round", () => {
    const source = readFileSync(join(import.meta.dir, "..", "api.ts"), "utf8");
    const posts = source.match(/call<[^>]+>\(\s*"POST",[^)]*\)/g) ?? [];
    expect(posts.length).toBeGreaterThan(0);
    for (const post of posts) expect(post).not.toMatch(/subtask|round/i);
  });

  test("the run window of a subtask loop shows the view and sends nothing", async () => {
    const m = mockFetch({
      "GET runs/run-1": () =>
        snapshot({
          state: "FORWARD_ACTIVE",
          reset_mode: "subtask_loop",
          episodes: { done: 1, total: 3, current: null },
          subtask: { phase: "forward", rounds_ended: 1, round: 2 },
          subtask_provider: { mode: "external", source: "frozen_plan" },
          requirements: requirements({
            reset_mode: "subtask_loop",
            subtask_loop: { subtask_max_retries: 5 },
          }),
        }),
      "GET runs/run-1/events?after=0&limit=200": () => ({
        events: [],
        next: 0,
      }),
      "GET runs/run-1/metrics": () => ({ reset_mode: "subtask_loop" }),
      "GET runs/run-1/subtasks": () => retry(),
    });
    restore = m.restore;
    const { host } = await render(<RunWindow runId="run-1" />);
    await waitFor(() =>
      host.querySelector('li.ar-subtask[data-status="running"]'),
    );
    expect(host.textContent).toContain("Subtask loop (no reset policy)");
    // episodes.done counts rounds in this mode.
    expect(host.textContent).toContain("Rounds 1 of 3");
    expect(row(host, "round")).toBe("2");
    expect(
      host.querySelector('[data-testid="subtask-provider"]')?.textContent,
    ).toBe("Configured answer providerExternal");
    expect(host.textContent).toContain("Reset (reverse-loop subtasks)");
    expect(host.textContent).toContain("Attempts: 2 (at most 5 resends)");
    expect(m.calls.filter((c) => c.method !== "GET")).toEqual([]);
    const buttons = Array.from(host.querySelectorAll("button")).map((b) =>
      b.textContent?.trim(),
    );
    // The stop button is the run window's own; nothing names a subtask or round.
    for (const name of buttons) expect(name).not.toMatch(/subtask|round/i);
  });

  test("another reset mode does not ask for subtasks", async () => {
    const m = mockFetch({
      "GET runs/run-1": () => snapshot({ state: "FORWARD_ACTIVE" }),
      "GET runs/run-1/events?after=0&limit=200": () => ({
        events: [],
        next: 0,
      }),
      "GET runs/run-1/metrics": () => ({}),
    });
    restore = m.restore;
    const { host } = await render(<RunWindow runId="run-1" />);
    await waitFor(() => host.querySelector("#ar-graph-title"));
    expect(m.calls.some((c) => c.path.includes("subtasks"))).toBe(false);
    expect(host.querySelector('[data-testid="subtask-loop"]')).toBeNull();
  });
});

describe("the requirements of a subtask-loop session", () => {
  test("its settings, and no reset policy or reset instruction", async () => {
    const { host } = await render(
      <RequirementsCard
        requirements={requirements({
          reset_mode: "subtask_loop",
          subtask_loop: {
            task: "cups_on_tray",
            rounds: 3,
            subtask_max_steps: 200,
            subtask_max_retries: 5,
            max_subtasks_per_loop: null,
            home_after_loop: true,
            vlm_min_free_mem_gib: 8,
          },
        })}
      />,
    );
    const got = Object.fromEntries(
      Array.from(host.querySelectorAll("[data-row]")).map((node) => [
        node.getAttribute("data-row"),
        node.querySelector("dd")?.textContent ?? "",
      ]),
    );
    expect(got.subtask_task).toBe("cups_on_tray");
    expect(got.subtask_rounds).toBe("3");
    expect(got.subtask_max_steps).toBe("200");
    expect(got.subtask_max_retries).toBe("5");
    expect(got.subtask_max_per_loop).toBe("Off (no limit)");
    expect(got.subtask_home).toBe("Yes");
    expect(got.subtask_vlm_mem).toBe("8 GiB");
    expect(got.reset_mode).toBe("Subtask loop (no reset policy)");
    expect(got.reset_policy).toBeUndefined();
    expect(got.reset_instruction).toBeUndefined();
    expect(got.reset_steps).toBeUndefined();
  });
});

describe("configured answer provider", () => {
  test.each([
    ["local", "Local"],
    ["external", "External"],
  ] as const)(
    "shows %s only as a frozen configuration declaration",
    async (mode, label) => {
      const { host } = await render(
        <SubtaskLoopView
          runId="run-1"
          data={happy()}
          provider={{ mode, source: "frozen_plan" }}
        />,
      );
      expect(
        host.querySelector('[data-testid="subtask-provider"] dd')?.textContent,
      ).toBe(label);
      expect(host.textContent).toContain(
        "does not verify the responding service",
      );
      expect(host.textContent).toContain("not a label");
      expect(host.textContent).not.toContain("the local model plans");
      expect(
        host.querySelectorAll("button, input, select, textarea, form").length,
      ).toBe(0);
    },
  );

  test.each([
    null,
    { mode: "unknown", source: "unavailable" },
    { mode: "external" },
    { mode: "external", source: "unavailable" },
    { mode: "bad", source: "frozen_plan" },
  ])(
    "missing or untrusted provider declaration stays unknown: %j",
    async (provider) => {
      const { host } = await render(
        <SubtaskLoopView
          runId="run-1"
          data={happy()}
          provider={provider as SubtaskProvider | null}
        />,
      );
      expect(
        host.querySelector('[data-testid="subtask-provider"] dd')?.textContent,
      ).toBe("Unknown");
    },
  );

  test.each([
    ["en", "Configured answer provider", "Unknown", "not a label"],
    ["zh", "配置的作答方式", "未知", "不是标签"],
  ])(
    "an older server is unknown in %s and the page stays read-only",
    async (language, title, value, note) => {
      const previous = window.localStorage.getItem("levi-language");
      window.localStorage.setItem("levi-language", language);
      const m = mockFetch({
        "GET runs/run-1": () =>
          snapshot({
            reset_mode: "subtask_loop",
            requirements: requirements({ reset_mode: "subtask_loop" }),
          }),
        "GET runs/run-1/events?after=0&limit=200": () => ({
          events: [],
          next: 0,
        }),
        "GET runs/run-1/metrics": () => ({ reset_mode: "subtask_loop" }),
        "GET runs/run-1/subtasks": () => happy(),
      });
      restore = m.restore;
      try {
        const { host } = await render(
          <LocaleProvider>
            <RunWindow runId="run-1" />
          </LocaleProvider>,
        );
        await waitFor(() =>
          host.querySelector('[data-testid="subtask-provider"]'),
        );
        const provider = host.querySelector('[data-testid="subtask-provider"]');
        expect(provider?.textContent).toBe(title + value);
        const view = host.querySelector('[data-testid="subtask-loop"]');
        expect(view?.textContent).toContain(note);
        expect(
          view?.querySelectorAll("button, input, select, textarea, form")
            .length,
        ).toBe(0);
        expect(m.calls.filter((call) => call.method !== "GET")).toEqual([]);
        expect(
          m.calls.some((call) => /binding|queue|provider/.test(call.path)),
        ).toBe(false);
      } finally {
        if (previous === null) window.localStorage.removeItem("levi-language");
        else window.localStorage.setItem("levi-language", previous);
      }
    },
  );
});

const requestEvidence: RequestEvidence = {
  seq: 44,
  visibility: "visible",
  context: {
    kind: "check-forward",
    round_id: "synthetic:round1:s1",
    episode_id: "synthetic.forward.0001",
  },
  report: {
    agent_id: "self-reported-agent",
    agent_notes: "<script>private-notes</script>",
    tokens: { input_new: 0, output: 7, cache_read: null, cache_write: 0 },
    client_latency_ms: 100,
    bridge_wait_ms: 90,
    self_reported_elapsed_ms: 80,
    prompt_text: "prompt marker",
    raw_content: "answer marker",
    prompt_text_truncated: false,
    raw_content_truncated: true,
    display_text_truncated: false,
    prompt_bytes: 100,
    answer_bytes: 200,
    prompt_sha256: null,
    answer_content_sha256: "a".repeat(64),
  },
};

const evidenceTokenLabels = {
  en: {
    input_new: "Self-reported new input tokens",
    output: "Self-reported output tokens",
    cache_read: "Self-reported cache-read tokens",
    cache_write: "Self-reported cache-write tokens",
  },
  zh: {
    input_new: "自报新增输入 token",
    output: "自报输出 token",
    cache_read: "自报缓存读取 token",
    cache_write: "自报缓存写入 token",
  },
} as const;
const evidenceTokenKeys = [
  "input_new",
  "output",
  "cache_read",
  "cache_write",
] as const;
const evidenceValue = (host: ParentNode, label: string) => {
  const terms = Array.from(host.querySelectorAll("dt")).filter(
    (term) => term.textContent === label,
  );
  expect(terms.length).toBe(1);
  const value = terms[0].nextElementSibling;
  expect(value?.tagName).toBe("DD");
  return value?.textContent;
};

describe("request evidence is a read-only server projection", () => {
  test.each(["en", "zh"])(
    "plain self-report, independent zero/unknown and limits (%s)",
    async (language) => {
      const before = window.localStorage.getItem("levi-language");
      window.localStorage.setItem("levi-language", language);
      try {
        const { host } = await render(
          <LocaleProvider>
            <SubtaskLoopView
              runId="synthetic"
              data={{
                ...happy(),
                request_evidence: [requestEvidence],
                request_evidence_truncated: true,
              }}
            />
          </LocaleProvider>,
        );
        const evidence = host.querySelector(
          '[data-testid="request-evidence"]',
        )!;
        expect(evidence.textContent).toContain("self-reported-agent");
        expect(evidence.textContent).toContain(
          "<script>private-notes</script>",
        );
        expect(evidence.querySelector("script")).toBeNull();
        expect(evidence.textContent).toContain(
          language === "zh" ? "自报作答者" : "Self-reported answerer",
        );
        expect(evidence.textContent).toContain(
          language === "zh" ? "未知" : "Unknown",
        );
        const labels =
          language === "zh" ? evidenceTokenLabels.zh : evidenceTokenLabels.en;
        expect(evidenceValue(evidence, labels.input_new)).toBe("0");
        expect(evidenceValue(evidence, labels.output)).toBe("7");
        expect(evidenceValue(evidence, labels.cache_read)).toBe(
          language === "zh" ? "未知" : "Unknown",
        );
        expect(evidenceValue(evidence, labels.cache_write)).toBe("0");
        const values = Array.from(
          evidence.querySelectorAll("dd"),
          (item) => item.textContent,
        );
        expect(values).toContain("100");
        expect(values).toContain("90");
        expect(values).toContain("80");
        expect(host.textContent).toContain("64,000");
        expect(
          evidence.querySelectorAll("button,input,textarea,a").length,
        ).toBe(0);
      } finally {
        if (before === null) window.localStorage.removeItem("levi-language");
        else window.localStorage.setItem("levi-language", before);
      }
    },
  );

  test.each(["en", "zh"])(
    "each token keeps its own null and zero value (%s)",
    async (language) => {
      const before = window.localStorage.getItem("levi-language");
      window.localStorage.setItem("levi-language", language);
      try {
        const base = happy();
        const view = (evidence: RequestEvidence) => (
          <LocaleProvider>
            <SubtaskLoopView
              runId="synthetic"
              data={{ ...base, request_evidence: [evidence] }}
            />
          </LocaleProvider>
        );
        const { host, rerender } = await render(view(requestEvidence));
        const labels =
          language === "zh" ? evidenceTokenLabels.zh : evidenceTokenLabels.en;
        const unknown = language === "zh" ? "未知" : "Unknown";
        for (const changed of evidenceTokenKeys) {
          for (const value of [null, 0]) {
            const tokens: NonNullable<RequestEvidence["report"]>["tokens"] = {
              input_new: 11,
              output: 22,
              cache_read: 33,
              cache_write: 44,
              [changed]: value,
            };
            await rerender(
              view({
                ...requestEvidence,
                report: { ...requestEvidence.report!, tokens },
              }),
            );
            const evidence = host.querySelector(
              '[data-testid="request-evidence"]',
            )!;
            for (const key of evidenceTokenKeys) {
              expect(evidenceValue(evidence, labels[key])).toBe(
                tokens[key] === null ? unknown : String(tokens[key]),
              );
            }
          }
        }
      } finally {
        if (before === null) window.localStorage.removeItem("levi-language");
        else window.localStorage.setItem("levi-language", before);
      }
    },
  );

  test.each(["en", "zh"])(
    "open evidence is removed on same-instance visible → hidden → missing updates (%s)",
    async (language) => {
      const before = window.localStorage.getItem("levi-language");
      window.localStorage.setItem("levi-language", language);
      try {
        const base = happy();
        expect("request_evidence" in base).toBe(false);
        const visible: RequestEvidence = {
          ...requestEvidence,
          report: { ...requestEvidence.report!, prompt_sha256: "b".repeat(64) },
        };
        const view = (data: SubtasksResponse) => (
          <LocaleProvider>
            <SubtaskLoopView runId="synthetic" data={data} />
          </LocaleProvider>
        );
        // This is a local component fixture, not a claimed API-produced projection.
        const { host, rerender } = await render(
          view({ ...base, request_evidence: [visible] }),
        );
        const panel = host.querySelector('[data-testid="request-evidence"]')!;
        const details = panel.querySelector("details")!;
        details.open = true;
        expect(details.open).toBe(true);
        const privateValues = [
          "self-reported-agent",
          "private-notes",
          "prompt marker",
          "answer marker",
          "b".repeat(64),
          "a".repeat(64),
        ];
        for (const value of privateValues)
          expect(panel.textContent).toContain(value);
        expect(details.querySelector("summary")?.textContent).toContain(
          `#${visible.seq}`,
        );
        await rerender(
          view({
            ...base,
            request_evidence: [
              {
                seq: visible.seq,
                visibility: "hidden",
                context: null,
                report: null,
              },
            ],
          }),
        );
        const hidden = host.querySelector('[data-testid="request-evidence"]')!;
        expect(hidden).toBe(panel);
        // Same seq/key and DOM node: remounting must not mask stale open content.
        expect(hidden.querySelector("details")).toBe(details);
        expect(details.open).toBe(true);
        expect(hidden.textContent).toContain(
          language === "zh" ? "保持隐藏" : "stays hidden",
        );
        expect(hidden.querySelectorAll("pre, code, dt, dd").length).toBe(0);
        for (const value of privateValues)
          expect(host.innerHTML).not.toContain(value);
        await rerender(view(base));
        expect(
          host.querySelector('[data-testid="request-evidence"]'),
        ).toBeNull();
        expect(host.contains(details)).toBe(false);
        for (const value of privateValues)
          expect(host.innerHTML).not.toContain(value);
      } finally {
        if (before === null) window.localStorage.removeItem("levi-language");
        else window.localStorage.setItem("levi-language", before);
      }
    },
  );

  test("hidden status cannot leak an accidentally supplied payload", async () => {
    const { host } = await render(
      <SubtaskLoopView
        runId="synthetic"
        data={{
          ...happy(),
          request_evidence: [{ ...requestEvidence, visibility: "hidden" }],
        }}
      />,
    );
    const evidence = host.querySelector('[data-testid="request-evidence"]')!;
    expect(evidence.textContent).not.toContain("private-notes");
    expect(evidence.textContent).not.toContain("prompt marker");
    expect(evidence.textContent).not.toContain("answer marker");
    expect(evidence.textContent).not.toContain("self-reported-agent");
  });

  test("old server has no request evidence panel", async () => {
    const { host } = await render(
      <SubtaskLoopView runId="synthetic" data={happy()} />,
    );
    expect(host.querySelector('[data-testid="request-evidence"]')).toBeNull();
  });
});
