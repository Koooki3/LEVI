import { describe, expect, test } from "bun:test";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import {
  ago,
  FINISHED_OK,
  pollDelay,
  statusTone,
  RUNNING,
  STOPPED,
} from "../pool-progress";
import { jobTarget } from "../job-list";
import type { PoolJob } from "../types";

const zhT = (key: string) =>
  (zh as Record<string, string>)[key.replace(/\s+/g, " ").trim()] ?? key;

describe("job states", () => {
  test("live and stopped sets", () => {
    for (const s of ["running", "stalled", "cancelling"])
      expect(RUNNING.has(s)).toBe(true);
    for (const s of ["done", "failed", "interrupted", "cancelled"])
      expect(RUNNING.has(s)).toBe(false);
    expect(STOPPED.has("interrupted")).toBe(true);
    expect(STOPPED.has("done_with_errors")).toBe(false);
    expect(STOPPED.has("done_with_warnings")).toBe(false);
    for (const s of ["done", "done_with_warnings", "done_with_errors"])
      expect(FINISHED_OK.has(s)).toBe(true);
    for (const s of ["failed", "cancelled", "interrupted", "running"])
      expect(FINISHED_OK.has(s)).toBe(false);
  });

  test("polling is fast while it moves, slower when stalled, never when over", () => {
    expect(pollDelay("running")).toBe(1000);
    expect(pollDelay("stalled")).toBe(2000);
    expect(pollDelay("cancelling")).toBe(2000);
    for (const s of ["done", "interrupted", "failed", "cancelled", "planned"])
      expect(pollDelay(s)).toBeNull();
  });

  test("tones", () => {
    expect(statusTone("done")).toBe("pass");
    expect(statusTone("interrupted")).toBe("fail");
    expect(statusTone("failed")).toBe("fail");
    expect(statusTone("stalled")).toBe("warn");
    expect(statusTone("done_with_errors")).toBe("warn");
    expect(statusTone("done_with_warnings")).toBe("warn");
    expect(statusTone("running")).toBe("");
  });

  test("how long ago, in both languages", () => {
    expect(ago(12, (k) => k)).toBe("12 s ago");
    expect(ago(300, (k) => k)).toBe("5 min ago");
    expect(ago(7200, (k) => k)).toBe("2 h ago");
    expect(ago(3 * 86400, (k) => k)).toBe("3 d ago");
    expect(ago(12, zhT)).toBe("12秒前");
    expect(ago(null, (k) => k)).toBe("—");
  });

  test("every state has a label in both catalogs", () => {
    for (const s of [
      "planned",
      "running",
      "stalled",
      "cancelling",
      "cancelled",
      "interrupted",
      "failed",
      "done",
      "done_with_warnings",
      "done_with_errors",
    ]) {
      expect((en as Record<string, string>)[s]).toBeTruthy();
      expect((zh as Record<string, string>)[s]).toBeTruthy();
    }
  });

  test("the list shows where a job wrote", () => {
    const base = { id: "x", status: "done" } as PoolJob;
    expect(
      jobTarget({ ...base, kind: "export", result: { dataset_path: "/a" } }),
    ).toBe("/a");
    expect(jobTarget({ ...base, kind: "export", target: "/b" })).toBe("/b");
    expect(jobTarget({ ...base, kind: "push", destination: "h:/c" })).toBe(
      "h:/c",
    );
    expect(jobTarget({ ...base, kind: "scan" })).toBe("");
  });
});
