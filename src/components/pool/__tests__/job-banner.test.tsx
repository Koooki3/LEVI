import { click, render, setupDom } from "../../ds/__tests__/dom";
import { afterEach, describe, expect, test } from "bun:test";
import { LocaleProvider } from "@/components/levi-locale";
import { JobBanner } from "../job-panel";
import type { PoolJob } from "../types";

setupDom();
afterEach(() => {
  try {
    localStorage.clear();
  } catch {
    // no storage in this DOM
  }
});

// What levi/pool/joblog.py writes for a worker that is no longer there.
const gone = {
  id: "export-1",
  kind: "export",
  status: "interrupted",
  // joblog.describe_exit: the reason is the same sentence as the message.
  reason:
    "The worker is gone: the service or the machine restarted, or the process was killed",
  resumable: true,
  rerunnable: true,
  age_seconds: 120,
  error_info: {
    type: "WorkerGone",
    message:
      "The worker is gone: the service or the machine restarted, or the process was killed",
    hint: "What finished is kept; resume continues.",
  },
} as unknown as PoolJob;

describe("a stopped job's note", () => {
  test("is the shared three-part box, with the job's own buttons in its fix", async () => {
    const { host } = await render(
      <JobBanner job={gone} onJob={() => {}} onLog={() => {}} />,
    );
    const box = host.querySelector(".pg-problem.pg-pool-banner")!;
    expect(box.querySelector(".pg-problem__title")!.textContent).toContain(
      "Interrupted",
    );
    expect(box.querySelector(".pg-problem__why")!.textContent).toContain(
      "The worker is gone",
    );
    expect(box.querySelector(".pg-problem__title")!.textContent).not.toContain(
      "The worker is gone",
    );
    const buttons = [...box.querySelectorAll(".pg-problem__fix button")].map(
      (b) => b.textContent,
    );
    expect(buttons).toContain("Resume");
    expect(buttons).toContain("View log");
  });

  test("a click on View log still reaches the page", async () => {
    let opened = 0;
    const { host } = await render(
      <JobBanner job={gone} onJob={() => {}} onLog={() => (opened += 1)} />,
    );
    const log = [...host.querySelectorAll("button")].find(
      (b) => b.textContent === "View log",
    )!;
    await click(log);
    expect(opened).toBe(1);
  });

  test("Chinese: the missing-worker note is written once, in Chinese", async () => {
    localStorage.setItem("levi-language", "zh");
    const { host } = await render(
      <LocaleProvider>
        <JobBanner job={gone} onJob={() => {}} onLog={() => {}} />
      </LocaleProvider>,
    );
    const text = host.textContent ?? "";
    expect(text).toContain("工作进程已经不在了");
    expect(text).toContain("已完成的部分会保留；可以继续。");
    expect(text).not.toContain("The worker is gone");
    expect(text).not.toContain("What finished");
    // Said once: the reason beside the title repeats the note below it.
    expect(text.split("工作进程已经不在了").length - 1).toBe(1);
  });
});
