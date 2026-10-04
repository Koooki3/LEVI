import { describe, expect, test } from "bun:test";
import {
  catalogDatasets,
  greeting,
  pendingTasks,
  recentDatasets,
  relativeTime,
  runningJobs,
} from "../home-data";

describe("home data", () => {
  test("pending tasks are the unfinished ones waiting for a person", () => {
    const body = {
      tasks: [
        {
          run_id: "a",
          waiting_for: "human_review",
          dataset: "d",
          episodes: [1, 2],
        },
        { run_id: "b", waiting_for: "agent_propose", dataset: "d" },
        // Finished runs can still wait for a person's review.
        { run_id: "c", waiting_for: "human_review", finished: true },
        { run_id: "f", waiting_for: "human_approval", status: "failed" },
        { run_id: "g", waiting_for: "human_commit", status: "cancelled" },
        { run_id: "d", waiting_for: "nothing; the annotations are published" },
        { run_id: "e", waiting_for: "human_commit", workflow: "temporal" },
      ],
    };
    const pending = pendingTasks(body);
    expect(pending.map((task) => task.runId)).toEqual(["a", "c", "e"]);
    expect(pending[0].episodes).toBe(2);
    expect(pendingTasks(null)).toEqual([]);
    expect(pendingTasks({ tasks: "x" })).toEqual([]);
  });

  test("running jobs come from both lists, finished ones left out", () => {
    const pool = {
      jobs: [
        {
          id: "p1",
          kind: "export",
          status: "running",
          options: { name: "plates" },
          progress: { stage: "copy", done: 3, total: 12 },
        },
        { id: "p2", kind: "scan", status: "succeeded" },
        { id: "p3", kind: "push", status: "stalled", destination: "/a/b/out/" },
      ],
    };
    const conversions = [
      { id: "c1", status: "queued", stage: "summary", source: "/x/raw" },
      { id: "c2", status: "failed" },
    ];
    const jobs = runningJobs(pool, conversions);
    expect(jobs.map((job) => job.id)).toEqual(["p1", "p3", "c1"]);
    expect(jobs[0]).toMatchObject({
      title: "Training pool export",
      subject: "plates",
      stage: "copy",
      fraction: 0.25,
      href: "/pool",
    });
    expect(jobs[1]).toMatchObject({
      subject: "out",
      stage: "stalled",
      fraction: null,
    });
    expect(jobs[2]).toMatchObject({
      kind: "conversion",
      subject: "raw",
      href: "/workbench",
    });
    expect(runningJobs(null, null)).toEqual([]);
  });

  test("recent datasets: visited first, then the other local ones", () => {
    const local = catalogDatasets({
      local: [
        { id: "local/a", name: "a", kind: "raw", info: { total_episodes: 24 } },
        { id: "local/b", name: "b", format: { episodes: 3 } },
        { name: "no id" },
      ],
    });
    expect(local).toEqual([
      { repo: "local/a", name: "a", episodes: 24, kind: "raw" },
      { repo: "local/b", name: "b", episodes: 3, kind: null },
    ]);
    const list = recentDatasets(
      [
        { repo: "local/b", episode: 2, at: 10 },
        { repo: "lerobot/x", episode: null, at: 5 },
      ],
      local,
    );
    expect(list.map((item) => item.repo)).toEqual([
      "local/b",
      "lerobot/x",
      "local/a",
    ]);
    expect(list[0]).toMatchObject({ name: "b", visitedAt: 10, episode: 2 });
    expect(list[2].visitedAt).toBeNull();
    expect(recentDatasets([], local, 1)).toHaveLength(1);
  });

  test("greeting and relative time", () => {
    expect(greeting(8)).toBe("Good morning.");
    expect(greeting(14)).toBe("Good afternoon.");
    expect(greeting(22)).toBe("Good evening.");
    expect(greeting(2)).toBe("Good evening.");
    expect(relativeTime(0, 30_000).key).toBe("just now");
    expect(relativeTime(0, 5 * 60_000)).toEqual({ key: "{n} min ago", n: 5 });
    expect(relativeTime(0, 3 * 3_600_000)).toEqual({ key: "{n} h ago", n: 3 });
    expect(relativeTime(0, 50 * 3_600_000)).toEqual({ key: "{n} d ago", n: 2 });
  });
});
