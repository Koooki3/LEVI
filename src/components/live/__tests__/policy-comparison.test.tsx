import { expect, test } from "bun:test";
import { render, setupDom } from "@/components/ds/__tests__/dom";
import { PolicyComparison } from "../policy-comparison";
import { resultFixture } from "./result-fixture";
setupDom();
test("explicit comparison retains policy, reset, scene, commit, environment and sample facts without ranking", async () => {
  const item = resultFixture({
    comparison: {
      status: "incompatible",
      campaign_id: "campaign-real",
      missing: ["unverified_arms"],
      differences: ["environment_versions"],
      conditions: {
        reset_mode: "manual",
        label_basis: "operator",
        stop_rules: {
          consecutive_faults: 3,
          unplanned_interventions_per_arm: 2,
        },
        runs: [
          {
            run_id: "eval-run",
            reset_mode: "manual",
            scene_check: "scene-ok",
            levi_commit: "commit-frozen",
            plan_sha256: "plan-hash",
          },
        ],
        arms: [
          {
            id: "policy-a",
            config: "config-a",
            checkpoint: "checkpoint-a",
            versions: "env-a",
            not_verified: [],
          },
          {
            id: "policy-b",
            config: "config-b",
            checkpoint: "checkpoint-b",
            versions: "env-b",
            not_verified: ["scene_check"],
          },
        ],
        coverage: {
          "policy-a": { valid: 10, labelled: 9, unlabelled: 1, coverage: 0.9 },
        },
        early_stop: {
          truth_basis: "operator",
          arms: {
            "policy-a": {
              episodes: 10,
              controls: 2,
              false_early_stop_rate: {
                rate: 0,
                k: 0,
                n: 2,
                status: "available",
              },
            },
          },
        },
      },
    },
  });
  const { host } = await render(<PolicyComparison item={item} />);
  for (const value of [
    "campaign-real",
    "Comparison conditions differ",
    "config-a",
    "config-b",
    "checkpoint-a",
    "checkpoint-b",
    "env-a",
    "env-b",
    "scene-ok",
    "commit-frozen",
    "plan-hash",
    "Label coverage",
    "90%",
    "False early stop rate",
  ])
    expect(host.textContent).toContain(value);
  expect(host.textContent).toContain("Policies are not ranked here");
  const { host: hidden } = await render(
    <PolicyComparison item={{ ...item, result_visibility: "withheld" }} />,
  );
  expect(hidden.textContent).not.toContain("checkpoint-a");
  expect(hidden.textContent).not.toContain("90%");
});
