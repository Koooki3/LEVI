import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import { LabellingQueue } from "../labelling-queue";
import {
  queueRowReason,
  queueRows,
  resumeText,
  yieldReasonText,
} from "../queue-logic";
import { ServicePanel } from "../service-panel";
import type { LiveStatusResponse, QueueRow, YieldInfo } from "../types";

const t = (key: string) => key;

const yielding: YieldInfo = {
  enabled: true,
  active: true,
  reasons: [
    { code: "deployment", text: "a policy server is listening on port 8000" },
    { code: "foreign_gpu", text: "other processes use 9000 MiB of the GPU" },
  ],
  since: 1_700_000_000,
  resume_at: null,
};
const rows: QueueRow[] = [
  {
    dataset: "a__t1",
    mode: "auto",
    waiting: 3,
    state: "yielding",
    reason: "x",
  },
  { dataset: "b__t2", mode: "history", waiting: 0, state: "done", reason: "" },
  {
    dataset: "c__t3",
    mode: "paused",
    waiting: 2,
    state: "paused",
    reason: "paused by a person; the queue is kept",
  },
  {
    dataset: "d__t4",
    mode: "auto",
    waiting: 1,
    state: "failed",
    reason: "vLLM failed",
  },
];

describe("queue rows and yield reasons in words", () => {
  test("a yield code is a plain sentence; an unknown code shows the server's text", () => {
    expect(yieldReasonText({ code: "deployment", text: "raw" }, t)).toBe(
      "A policy server or an evaluation session is running on the robot side.",
    );
    expect(yieldReasonText({ code: "weird", text: "raw text" }, t)).toBe(
      "raw text",
    );
    expect(yieldReasonText({ code: "weird" }, t)).toBe("weird");
  });

  test("a yielding row gives the yield reasons, not the raw status text", () => {
    const text = queueRowReason(rows[0], yielding, t);
    expect(text).toContain("A policy server or an evaluation session");
    expect(text).toContain("Another program is using the GPU");
    expect(text).not.toContain("9000");
  });

  test("a paused row is told in words and an unknown reason stays as sent", () => {
    expect(queueRowReason(rows[2], yielding, t)).toBe(
      "Paused by a person; the waiting episodes are kept.",
    );
    expect(queueRowReason(rows[3], null, t)).toBe("vLLM failed");
    expect(queueRowReason(rows[1], null, t)).toBe("");
  });

  test("the recovery condition names a time when the service gave one", () => {
    expect(resumeText(yielding, t)).toBe(
      "Labelling resumes by itself once every reason has gone and stayed gone for the configured wait.",
    );
    const text = resumeText({ ...yielding, resume_at: 1_700_000_100 }, t);
    expect(text).toContain("resume");
    expect(text).toMatch(/\d/);
    expect(resumeText({ active: false }, t)).toBe("");
    expect(resumeText(null, t)).toBe("");
  });

  test("rows are shown as sent, most urgent first, and a malformed row is dropped", () => {
    const got = queueRows([
      ...rows,
      { dataset: "" } as QueueRow,
      null as unknown as QueueRow,
    ]);
    expect(got.map((r) => r.dataset)).toEqual([
      "d__t4",
      "a__t1",
      "c__t3",
      "b__t2",
    ]);
  });
});

describe("the labelling queue on the service panel", () => {
  const status = (service: Record<string, unknown>) =>
    ({
      alive: true,
      service: { state: "gpu_wait", gpu: {}, resources: {}, ...service },
    }) as unknown as LiveStatusResponse;

  test("lists each dataset with its state, why it waits and when it resumes", () => {
    const html = renderToStaticMarkup(
      <ServicePanel
        status={status({ queue: rows, yield: yielding, queue_depth: 2 })}
        alive
        now={1_700_000_050}
      />,
    );
    expect(html).toContain("a__t1");
    expect(html).toContain("Yielding the GPU");
    expect(html).toContain("A policy server or an evaluation session");
    expect(html).toContain("Another program is using the GPU");
    expect(html).toContain("Labelling resumes by itself");
    expect(html).toContain("Paused by a person");
    expect(html).toContain("vLLM failed");
  });

  test("an older status file without queue rows shows the old count only", () => {
    const html = renderToStaticMarkup(
      <ServicePanel
        status={status({ queue_depth: 2 })}
        alive
        now={1_700_000_050}
      />,
    );
    expect(html).toContain("dataset(s) waiting");
    expect(html).not.toContain("Yielding the GPU");
  });

  test("not yielding says so without a recovery line", () => {
    const html = renderToStaticMarkup(
      <LabellingQueue rows={rows.slice(1, 2)} yieldInfo={{ active: false }} />,
    );
    expect(html).not.toContain("resumes by itself");
    expect(html).toContain("b__t2");
  });
});
