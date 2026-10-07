import { click, render, setupDom, waitFor } from "../../ds/__tests__/dom";
import { afterEach, describe, expect, mock, test } from "bun:test";
import { LocaleProvider } from "@/components/levi-locale";
import en from "@/i18n/en.json";
import { JobDetailsDialog, shortEpisode } from "../job-details";
import { JobBanner } from "../job-panel";
import { jobNotes } from "../job-list";
import type { JobDetails, PoolJob } from "../types";

setupDom();

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
  try {
    localStorage.clear();
  } catch {
    // no storage in this DOM
  }
});

const DETAILS: JobDetails = {
  id: "export-1",
  kind: "export",
  status: "done_with_warnings",
  summary: { episodes: 1497, frames: 156655, bytes: 3874059685 },
  warnings: [
    {
      code: "copy_task_conflict",
      blocking: false,
      count: 30,
      episodes: ["/d/e/demo_0001", "/d/e/demo_0002"],
      episodes_total: 30,
    },
    { code: "reset_unreviewed", message: "Releases are judged by image only" },
  ],
  left_out: {
    total: 3,
    groups: [
      { code: "stale_state", count: 2 },
      { code: "stall_markers", count: 1 },
    ],
    items: [
      {
        episode: "/data/task_a/demo_0005",
        stage: "Convert raw captures",
        message: "Stale state source timestamp run: 61",
        reasons: ["stale_state"],
      },
      {
        episode: "/data/task_a/demo_0006",
        stage: "Convert raw captures",
        message: "Stale state source timestamp run: 7",
        reasons: ["stale_state"],
      },
      {
        episode: "/data/task_b/demo_0001",
        stage: "Convert raw captures",
        message: "Capture metadata records camera stall",
        reasons: ["stall_markers"],
      },
    ],
  },
  failed: { total: 0, items: [] },
};

function serve(body: unknown, status = 200) {
  const calls: string[] = [];
  globalThis.fetch = mock((input: RequestInfo | URL) => {
    calls.push(String(input));
    return Promise.resolve(
      new Response(JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
      }),
    );
  }) as unknown as typeof fetch;
  return calls;
}

const job = (extra: Partial<PoolJob> = {}) =>
  ({
    id: "export-1",
    kind: "export",
    status: "done_with_warnings",
    ...extra,
  }) as PoolJob;

describe("a job's details", () => {
  test("an episode is named by its task folder and its own folder", () => {
    expect(shortEpisode("/a/b/c/task_x/demo_0001")).toBe("task_x/demo_0001");
    expect(shortEpisode("demo_1")).toBe("demo_1");
  });

  test("the dialog reads the job's details and groups the left-out episodes by reason", async () => {
    const calls = serve(DETAILS);
    const { host } = await render(
      <JobDetailsDialog job={job()} onClose={() => {}} />,
    );
    await waitFor(() => host.textContent?.includes("Warnings"));
    expect(calls[0]).toContain("/pool/jobs/export-1/details");
    const text = host.textContent ?? "";
    expect(text).toContain("1497 episodes");
    expect(text).toContain("Episodes left out by data checks (3)");
    expect(text).toContain(
      "2 ×The robot state clock stopped updating (the readings were frozen)".replace(
        "2 ×",
        "2 × ",
      ),
    );
    expect(text).toContain("1 × The collector recorded a camera stall");
    // The warning without a sentence of its own is explained; the other keeps its text.
    expect(text).toContain("Copies of one recording have different task texts");
    expect(text).toContain("Releases are judged by image only");
    // Every episode is listed with its message, behind a fold.
    expect(text).toContain("task_a/demo_0005");
    expect(text).toContain("Stale state source timestamp run: 61");
    expect(text).toContain("the export itself is complete");
  });

  test("Chinese: reasons and headings are in Chinese", async () => {
    localStorage.setItem("levi-language", "zh");
    serve(DETAILS);
    const { host } = await render(
      <LocaleProvider>
        <JobDetailsDialog job={job()} onClose={() => {}} />
      </LocaleProvider>,
    );
    await waitFor(() => host.textContent?.includes("警告"));
    const text = host.textContent ?? "";
    expect(text).toContain("被数据检查剔除的片段");
    expect(text).toContain("机器人状态的时间戳不再更新");
    expect(text).toContain("作业详情");
  });

  test("a real failure shows its traceback; a job with nothing to say says so", async () => {
    serve({
      ...DETAILS,
      status: "done_with_errors",
      warnings: [],
      left_out: { total: 0, groups: [], items: [] },
      failed: {
        total: 1,
        items: [
          {
            episode: "/d/t/demo_0009",
            stage: "Convert raw captures",
            type: "ValueError",
            message: "encoder exploded",
            traceback: "Traceback...\nValueError: encoder exploded",
          },
        ],
      },
    });
    const first = await render(
      <JobDetailsDialog job={job()} onClose={() => {}} />,
    );
    await waitFor(() => first.host.textContent?.includes("failed to convert"));
    expect(first.host.textContent).toContain("ValueError");
    expect(first.host.textContent).toContain("Traceback...");
    serve({
      ...DETAILS,
      status: "done",
      warnings: [],
      left_out: { total: 0, groups: [], items: [] },
    });
    const second = await render(
      <JobDetailsDialog job={job({ id: "export-2" })} onClose={() => {}} />,
    );
    await waitFor(() => second.host.textContent?.includes("Nothing to report"));
  });

  test("a request that fails is a message with a retry, not a blank dialog", async () => {
    serve({ detail: "Pool job not found" }, 404);
    const { host } = await render(
      <JobDetailsDialog job={job()} onClose={() => {}} />,
    );
    await waitFor(() =>
      host.textContent?.includes("The job details could not be read"),
    );
  });
});

describe("the dialog's state", () => {
  test("an answer for the job that was closed is not shown under the next one", async () => {
    const answers = new Map<string, (r: Response) => void>();
    globalThis.fetch = mock((input: RequestInfo | URL) => {
      const url = String(input);
      return new Promise<Response>((resolve) => {
        answers.set(url.includes("export-a") ? "a" : "b", resolve);
      });
    }) as unknown as typeof fetch;
    const reply = (id: string, status: string) =>
      new Response(JSON.stringify({ ...DETAILS, id, status }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    const { host, rerender } = await render(
      <JobDetailsDialog job={job({ id: "export-a" })} onClose={() => {}} />,
    );
    await rerender(
      <JobDetailsDialog job={job({ id: "export-b" })} onClose={() => {}} />,
    );
    await waitFor(() => answers.has("b"));
    answers.get("b")!(reply("export-b", "done_with_warnings"));
    await waitFor(() => host.textContent?.includes("Warnings"));
    answers.get("a")!(reply("export-a", "done_with_errors"));
    await new Promise((r) => setTimeout(r, 30));
    expect(host.textContent).toContain("done (with warnings)");
    expect(host.textContent).not.toContain("done (with errors)");
  });

  test("the same episode listed twice does not break the list", async () => {
    const dup = DETAILS.left_out.items[0];
    serve({
      ...DETAILS,
      left_out: { ...DETAILS.left_out, items: [dup, dup] },
    });
    const { host } = await render(
      <JobDetailsDialog job={job()} onClose={() => {}} />,
    );
    await waitFor(() => host.textContent?.includes("Warnings"));
    expect((host.textContent ?? "").split("task_a/demo_0005").length - 1).toBe(
      2,
    );
  });
});

describe("the notes beside a finished job", () => {
  // The English catalog as the page uses it (a key without an entry is shown as is).
  const t = (k: string) => (en as Record<string, string>)[k] ?? k;
  test("only what there is, in order", () => {
    expect(jobNotes(job(), t)).toBe("");
    expect(jobNotes(job({ left_out: 26, warning_count: 2 }), t)).toBe(
      "26 left out · 2 warnings",
    );
    expect(jobNotes(job({ left_out: 1, failed: 2 }), t)).toBe(
      "1 left out · 2 failed",
    );
  });
});

describe("the note of a finished export that left episodes out", () => {
  test("is a warning with a details button and without the stop/repeat buttons", async () => {
    serve(DETAILS);
    const { host } = await render(
      <JobBanner
        job={job({
          rerunnable: true,
          left_out: 26,
          result: { errors: 26, left_out: 26, failed: 0 },
        })}
        onJob={() => {}}
        onLog={() => {}}
      />,
    );
    const box = host.querySelector(".pg-problem.pg-pool-banner")!;
    expect(box.querySelector(".pg-problem__title")!.textContent).toContain(
      "Finished with warnings",
    );
    expect(box.querySelector(".pg-problem__why")!.textContent).toContain(
      "26 episode(s) were left out by data checks",
    );
    const buttons = [...box.querySelectorAll(".pg-problem__fix button")].map(
      (b) => b.textContent,
    );
    expect(buttons).toContain("View details");
    expect(buttons).not.toContain("Re-run");
    expect(buttons).not.toContain("Cancel");
    const open = [...box.querySelectorAll("button")].find(
      (b) => b.textContent === "View details",
    )!;
    await click(open);
    await waitFor(() => host.textContent?.includes("Episodes left out"));
  });
});
