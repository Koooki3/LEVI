import { describe, expect, test } from "bun:test";
import {
  STATE_LANES,
  currentEpisodeText,
  eventReason,
  localDuration,
  metricGroups,
  metricName,
  metricValue,
  isBlind,
  maskEvent,
  mergeCard,
  metricRows,
  movesRobot,
  nextPollDelay,
  resultDone,
  resultKey,
  sceneAnswers,
  sceneComplete,
  sceneSecondsLeft,
  stateGraph,
  visibleEnding,
  visibleVerdict,
} from "../run-logic";
import { card } from "./fixtures";

const nodes = (state: string, mode: "human_assisted" | "single_reset_policy") =>
  stateGraph(state, mode).flatMap((lane) => lane.nodes);

describe("the state graph", () => {
  test("has the 13 states of the state machine, each once", () => {
    const all = STATE_LANES.flatMap((lane) => lane.states);
    expect(all.length).toBe(13);
    expect(new Set(all).size).toBe(13);
  });
  test("marks the current state", () => {
    const current = nodes("FORWARD_ACTIVE", "single_reset_policy").filter(
      (n) => n.status === "current",
    );
    expect(current.map((n) => n.state)).toEqual(["FORWARD_ACTIVE"]);
  });
  test("greys the three reset states in a manual-reset run, not in a policy run", () => {
    const manual = nodes("WAIT_HUMAN", "human_assisted");
    expect(
      manual.filter((n) => n.status === "disabled").map((n) => n.state),
    ).toEqual(["RESET_ACTIVE", "RESET_VERIFY", "RESET_FINALIZE"]);
    expect(
      nodes("WAIT_HUMAN", "single_reset_policy").some(
        (n) => n.status === "disabled",
      ),
    ).toBe(false);
  });
  test("a state the page does not know still shows as current", () => {
    const lanes = stateGraph("INIT", "human_assisted");
    expect(lanes[0].id).toBe("other");
    expect(lanes[0].nodes[0]).toEqual({ state: "INIT", status: "current" });
  });
  test("only a dry run or a shadow run keeps the robot still", () => {
    expect(movesRobot("dry_run")).toBe(false);
    expect(movesRobot("shadow")).toBe(false);
    expect(movesRobot("assisted")).toBe(true);
    expect(movesRobot("autonomous")).toBe(true);
  });
});

describe("blind labels", () => {
  test("a card is blind until the server says it is not", () => {
    expect(isBlind(card())).toBe(true);
    expect(
      isBlind(
        card({
          operator_label: {
            current: "success",
            automatic_verdict: "success",
            hidden_until_labelled: false,
          },
        }),
      ),
    ).toBe(false);
    // A missing flag counts as blind.
    const odd = card();
    delete (odd.operator_label as { hidden_until_labelled?: boolean })
      .hidden_until_labelled;
    expect(isBlind(odd)).toBe(true);
  });
  test("no verdict and no ending while blind", () => {
    const blind = card();
    expect(visibleVerdict(blind)).toBeNull();
    expect(visibleEnding(blind)).toEqual({
      endedBy: null,
      stopReason: null,
      rolloutLabel: null,
    });
  });
  test("after the label the verdict and the ending are visible", () => {
    const open = card({
      operator_label: {
        current: "failure",
        automatic_verdict: "success",
        hidden_until_labelled: false,
      },
    });
    expect(visibleVerdict(open)).toBe("success");
    expect(visibleEnding(open).endedBy).toBe("early_stop");
  });
  test("a poll that is still blind does not hide the label just posted", () => {
    const posted = card({
      operator_label: {
        current: "failure",
        automatic_verdict: "success",
        hidden_until_labelled: false,
      },
    });
    expect(mergeCard(card(), posted)).toBe(posted);
    // Another episode: the poll wins.
    const next = card({
      last_episode: { episode_id: "ep-0008", ended_by: null },
    });
    expect(mergeCard(next, posted)).toBe(next);
    expect(mergeCard(null, posted)).toBeNull();
    expect(mergeCard(card(), null)?.last_episode?.episode_id).toBe("ep-0007");
  });
  test("events that tell how an episode ended are masked while blind", () => {
    const ev = {
      seq: 3,
      at: 1,
      kind: "note",
      reason: "early_stop goal_verified",
    };
    expect(maskEvent(ev, true).kind).toBe("hidden");
    expect(maskEvent(ev, true).reason).toBeNull();
    expect(maskEvent(ev, false)).toBe(ev);
    const plain = {
      seq: 4,
      at: 1,
      kind: "transition",
      reason: "scene_unknown",
    };
    expect(maskEvent(plain, true)).toBe(plain);
  });
  test("the agreement group of the metrics is left out while blind", () => {
    const report = {
      reset_mode: "human_assisted",
      comparable: ["agreement"],
      agreement: {
        judged: 5,
        agreement: { n: 4, of: 5, rate: 0.8, wilson95: [0.4, 0.97] },
      },
      autonomous: { success: { n: 3, of: 6, rate: 0.5, wilson95: [0.2, 0.8] } },
    };
    expect(metricRows(report, true).map((r) => r.path)).toEqual([
      "autonomous.success",
    ]);
    const open = metricRows(report, false);
    expect(open.some((r) => r.group === "agreement")).toBe(true);
    const rate = open.find((r) => r.path === "agreement.agreement")!;
    expect(rate.interval).toEqual([0.4, 0.97]);
    expect(rate.comparable).toBe(true);
    expect(rate.value).toBe("4 / 5 (80.0 %)");
  });
});

describe("results and polling", () => {
  test("a code the page knows gets its own message", () => {
    expect(resultKey({ result: "refused", code: "stale_sequence" })).toBe(
      "automatic.run.result.stale_sequence",
    );
    expect(
      resultKey({ result: "refused", code: "confirmations_missing" }),
    ).toBe("automatic.run.result.confirmations_missing");
    expect(resultKey({ result: "refused" })).toBe(
      "automatic.run.result.refused",
    );
    expect(resultKey(null)).toBeNull();
    expect(resultDone({ result: "repeated" })).toBe(true);
    expect(resultDone({ result: "refused" })).toBe(false);
  });
  test("2 s while the run goes, slower hidden or finished, doubling on failure", () => {
    expect(nextPollDelay({ hidden: false, state: "FORWARD_ACTIVE" })).toBe(
      2000,
    );
    expect(nextPollDelay({ hidden: true })).toBeGreaterThan(5000);
    expect(nextPollDelay({ hidden: false, state: "COMPLETED" })).toBe(10000);
    expect(nextPollDelay({ hidden: false, failures: 2 })).toBe(8000);
    expect(nextPollDelay({ hidden: false, failures: 20 })).toBe(30000);
  });
});

describe("the scene question", () => {
  test("answers carry only the predicates named, unanswered optional ones as unclear", () => {
    expect(
      sceneAnswers(["a", "b", "c"], { a: true, b: false, junk: true } as never),
    ).toEqual({ a: true, b: false, c: null });
  });
  test("complete when every required predicate has an answer, null counts", () => {
    expect(sceneComplete(["a", "b"], { a: true })).toBe(false);
    expect(sceneComplete(["a", "b"], { a: true, b: null })).toBe(true);
  });
  test("seconds left never go below zero", () => {
    expect(sceneSecondsLeft(1000, 60, 1000)).toBe(60);
    expect(sceneSecondsLeft(1000, 60, 1000 + 61_000)).toBe(0);
  });
});

describe("the metrics in words (P5)", () => {
  const t = (key: string) =>
    ({
      "automatic.metric.seg.automation": "Automation",
      "automatic.metric.seg.unplanned_share":
        "Share of unplanned interventions",
      "automatic.metric.seg.scene_ms": "Scene check time",
      "automatic.metric.seg.mean": "average",
      "automatic.metric.no_data": "No data yet",
      "automatic.duration.ms": "{ms} ms",
      "automatic.duration.s": "{s} s",
      "automatic.duration.min": "{m} min {s} s",
    })[key] ?? key;
  const report = {
    reset_mode: "human_assisted",
    comparable: ["automation.unplanned"],
    truth_labels: { task_outcome: 1 },
    automation: {
      unplanned: 0,
      unplanned_share: { n: 0, of: 0, rate: null, wilson95: null },
      person_ms: { total: 90000, open_waits: 0 },
    },
    turnaround: { scene_ms: { mean: 450, n: 2 } },
    reset: { resets: 0 },
  };
  test("sorts rows into the named groups and drops the empty ones", () => {
    const groups = metricGroups(metricRows(report, false));
    expect(groups.map((g) => g.id)).toEqual([
      "overview",
      "intervention",
      "reset",
    ]);
    expect(groups[1].rows.map((r) => r.path)).toContain(
      "turnaround.scene_ms.mean",
    );
  });
  test("names a metric by its words, without the group key", () => {
    expect(metricName("turnaround.scene_ms.mean", t)).toBe(
      "Scene check time · average",
    );
    // A segment the catalogue lacks keeps its own words, not an underscore.
    expect(metricName("automation.some_new_count", t)).toBe("some new count");
  });
  test("times become durations, counts stay counts, and 0 of 0 says no data", () => {
    const rows = metricRows(report, false);
    const row = (path: string) => rows.find((r) => r.path === path)!;
    expect(metricValue(row("turnaround.scene_ms.mean"), t)).toBe("450 ms");
    expect(metricValue(row("automation.person_ms.total"), t)).toBe(
      "1 min 30 s",
    );
    expect(metricValue(row("automation.person_ms.open_waits"), t)).toBe("0");
    expect(metricValue(row("automation.unplanned"), t)).toBe("0");
    expect(metricValue(row("automation.unplanned_share"), t)).toBe(
      "No data yet",
    );
  });
  test("durations use the catalogue's words", () => {
    expect(localDuration(5000, t)).toBe("5 s");
    expect(localDuration(352000, t)).toBe("5 min 52 s");
  });
});

describe("events and the current episode in words (P4)", () => {
  test("a code the page knows is translated, an unknown one is kept", () => {
    const t = (key: string) =>
      key === "automatic.event.reason.scene_ready" ? "The scene is ready" : key;
    expect(eventReason("scene_ready", t)).toBe("The scene is ready");
    expect(eventReason("brand_new_code", t)).toBe("brand_new_code");
  });
  test("the episode is the id the service sends, or the number of an object", () => {
    expect(currentEpisodeText("cli-lab2.forward.0001")).toBe(
      "cli-lab2.forward.0001",
    );
    expect(currentEpisodeText({ no: 7, step: 12 })).toBe("7");
    expect(currentEpisodeText(null, "ep-prev")).toBe("ep-prev");
    expect(currentEpisodeText(null)).toBeNull();
  });
});
