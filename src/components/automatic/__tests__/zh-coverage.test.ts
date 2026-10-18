import { describe, expect, test } from "bun:test";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import { eventReason } from "../run-logic";
import { modeReasonText } from "../wizard-steps";

const enText = (key: string) => (en as Record<string, string>)[key] ?? key;
const zhText = (key: string) => (zh as Record<string, string>)[key] ?? key;

// `TRANSITION_REASONS` of levi/domain/aeri.py: every code a journal line can
// carry must have words in both languages, not the raw code.
const TRANSITION_REASONS = [
  "inference_paused",
  "inference_resumed",
  "judgement_unproven",
  "goal_verified",
  "horizon_exhausted",
  "operator_stop",
  "safety_stop",
  "policy_error",
  "watchdog_timeout",
  "recovery_ambiguous",
  "journal_corrupt",
  "preflight_passed",
  "preflight_failed",
  "robot_home_reached",
  "scene_ready",
  "scene_reset_required",
  "scene_unknown",
  "reset_horizon_exhausted",
  "reset_verified",
  "home_failed",
  "recorder_failed",
  "human_resumed",
  "run_completed",
  "contract_violation_limit",
  "home_not_executed",
  "home_pose_not_reached",
  "camera_stale",
  "operator_verdict",
  "subtask_dispatched",
  "subtask_continue",
  "subtask_ended",
  "subtask_forward_done",
  "subtask_reverse_done",
  "subtask_check_unavailable",
  "subtask_retry_limit",
  "subtask_plan_refused",
  "subtask_inventory_rejected",
  "subtask_not_released",
  "vlm_unavailable",
  "subtask_early_check",
];

describe("every journal reason has words in both languages", () => {
  test.each(TRANSITION_REASONS)("%s", (code) => {
    const english = eventReason(code, enText);
    const chinese = eventReason(code, zhText);
    expect(english).not.toBe(code);
    expect(chinese).not.toBe(code);
    expect(chinese).toMatch(/[一-鿿]/);
  });
});

describe("the server's unavailable-mode reason in words", () => {
  const server =
    "no real robot adapter in this version (only a dry run can launch)";
  test("a known sentence is translated", () => {
    expect(modeReasonText(server, zhText)).toMatch(/[一-鿿]/);
    expect(modeReasonText(server, enText)).toContain("dry run");
  });
  test("an unknown sentence stays as the server sent it", () => {
    expect(modeReasonText("something new", zhText)).toBe("something new");
    expect(modeReasonText(undefined, zhText)).toBe(
      zhText("automatic.wizard.settings.mode_unavailable"),
    );
  });
});
