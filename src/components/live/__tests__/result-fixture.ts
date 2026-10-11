import type { ResultItem } from "../result-types";
const share = {
  n: 1,
  of: 2,
  rate: 0.5,
  wilson95: [0.1, 0.9] as [number, number],
  small_sample: true,
};
export function resultFixture(overrides: Partial<ResultItem> = {}): ResultItem {
  return {
    identity: {
      root: "/fixture",
      workspace: "/fake-workspace",
      group: "model",
      task: "task",
      session_id: "session-1",
      run_id: "run-1",
      datasets: ["model__task"],
    },
    status: "reported",
    reason: null,
    result_visibility: "visible",
    source: {
      kind: "current_session",
      snapshot: "snapshot-hash",
      basis: "operator_and_automatic_separate",
      missing: [],
    },
    policy: { config: "policy-config", checkpoint: "checkpoint-1" },
    label_mode: "dual_label",
    analysis: {
      schema: "levi.live.analysis.summary.v1",
      cells: [],
      all: {
        episodes: 2,
        coverage: { operator: share, background: share, segments: share },
        success: share,
        background_success: share,
        funnel: {
          of: 2,
          stages: [{ id: "grasp", reached: share, present: 1 }],
        },
        warnings: [{ code: "small_sample", n: 2, min: 10 }],
      },
    },
    records: [
      {
        dataset: "model__task",
        demo: "demo-7",
        episode_index: 7,
        session: "run-1",
        operator: { outcome: "success" },
        online: { outcome: "failure" },
        agent: { outcome: "failure", review: "auto", rule: "release-rule" },
        segments: {
          count: 1,
          items: [{ id: "grasp", start: 0, end: 2, outcome: "success" }],
        },
        spec: {
          guideline: "generic-v2",
          release_review: "release",
          release_review_version: "v3",
        },
      },
    ],
    viewers: [
      {
        dataset: "model__task",
        repo_id: "local/live.model__task",
        native_repo_id: "local/model__task",
        viewer_href: "/local/live.model__task/episode_7?live_session=run-1",
        annotation_base: null,
        status: "ready",
      },
    ],
    runs: [
      {
        dataset: "model__task",
        kind: "temporal",
        run_id: "annotation-1",
        changeset: "change-1",
        repo_id: "local/model__task",
        viewer_href: "/local/live.model__task/episode_7?live_session=run-1",
        annotation_href: "/local/model__task/episode_7?agent_run=annotation-1",
        episodes: [7],
      },
      {
        dataset: "model__task",
        kind: "verdict",
        run_id: "annotation-2",
        changeset: null,
        repo_id: "local/model__task",
        viewer_href: "/local/live.model__task/episode_7?live_session=run-1",
        annotation_href: "/local/model__task/episode_7?agent_run=annotation-2",
        episodes: [7],
      },
    ],
    reports: [
      {
        basis: "operator",
        version: "report-v1",
        files: ["report.json"],
        current: false,
        status: "stale",
      },
    ],
    comparison: {
      status: "unknown",
      campaign_id: null,
      missing: ["campaign_id", "reset"],
    },
    ...overrides,
  };
}
