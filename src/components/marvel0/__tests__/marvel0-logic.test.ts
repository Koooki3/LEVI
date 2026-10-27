import { describe, expect, test } from "bun:test";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import {
  RECENT_S,
  MARVEL0_FRESH_MS,
  MARVEL0_SCHEMA,
  MARVEL0_POLL_MAX_MS,
  MARVEL0_POLL_MS,
  MARVEL0_REQUEST_MS,
  STACK_ORDER,
  STATE_KEY,
  STATE_TONE,
  isFresh,
  marvel0Delay,
  marvel0View,
  sampleAgeS,
  type Marvel0Card,
  type Marvel0CardId,
  type Marvel0State,
  type Marvel0View,
  type Say,
} from "../marvel0-logic";
import { sayText } from "../marvel0-say";
import quietSample from "./marvel0-status.quiet.sample.json";
import {
  gpuPart,
  healthPart,
  livePart,
  portsPart,
  quietMarvel0,
  quietSetup,
  marvel0Status,
  setupStatus,
} from "./fixtures";

const STATES: Marvel0State[] = ["ok", "red", "stale", "not_started", "unknown"];

function cards(view: Marvel0View): Record<Marvel0CardId, Marvel0Card> {
  const found = {} as Record<Marvel0CardId, Marvel0Card>;
  for (const group of view.groups)
    for (const card of group.cards) found[card.id] = card;
  return found;
}
const view = (setup: unknown, marvel0: unknown = marvel0Status()) =>
  marvel0View({ setup, marvel0 });
const card = (
  id: Marvel0CardId,
  setup: unknown,
  marvel0: unknown = marvel0Status(),
) => cards(view(setup, marvel0))[id];

/** Every piece of text a Say can come to, as one string (keys unexpanded). */
function flat(say: Say): string {
  if ("raw" in say) return say.raw;
  if ("ago" in say) return `ago:${say.ago}`;
  if ("join" in say) return say.join.map(flat).join(" | ");
  return say.params ? `${say.key} ${JSON.stringify(say.params)}` : say.key;
}
const isSay = (note: unknown): note is Say =>
  typeof note === "object" && note !== null;
/** The notes of a card that are text a server wrote. */
const serverNotes = (c: Marvel0Card) =>
  c.notes.filter((note): note is string => typeof note === "string");
const summary = (c: Marvel0Card) => flat(c.summary);
const factText = (c: Marvel0Card, label: string) =>
  c.facts
    .filter((fact) => {
      const shown = flat(fact.label);
      return shown === label || shown.startsWith(`${label} `);
    })
    .map((fact) => flat(fact.value));

describe("a running marvel0", () => {
  test("every component is ok except the gripper stack, which has no passive source", () => {
    const found = cards(view(setupStatus()));
    const states = Object.fromEntries(
      Object.values(found).map((c) => [c.id, c.state]),
    );
    expect(states).toEqual({
      desk: "ok",
      arm: "ok",
      gripper: "unknown",
      robot_server: "ok",
      monitor: "ok",
      policy: "ok",
      demo: "ok",
      live: "ok",
      vlm: "ok",
      gpu: "ok",
    });
  });

  test("the cards come in the page's steps, arm stack first in the strip's order, each with a hint", () => {
    const result = view(setupStatus());
    expect(result.groups.map((g) => g.id)).toEqual([
      "arm",
      "policy",
      "client",
      "host",
    ]);
    expect(result.groups.map((g) => g.cards.map((c) => c.id))).toEqual([
      ["monitor", "desk", "arm", "gripper", "robot_server"],
      ["policy"],
      ["demo"],
      ["live", "vlm", "gpu"],
    ]);
    for (const c of Object.values(cards(result))) {
      expect(c.hint).toBeTruthy();
      expect(STATES).toContain(c.state);
    }
    expect(result.notes).toEqual([]);
  });

  test("a marvel0 with nothing running is not started, and says idle", () => {
    const result = view(quietSetup(), quietMarvel0());
    const found = cards(result);
    expect(found.robot_server.state).toBe("not_started");
    expect(found.policy.state).toBe("not_started");
    expect(found.monitor.state).toBe("not_started");
    expect(found.demo.state).toBe("not_started");
    expect(found.desk.state).toBe("unknown");
    expect(found.arm.state).toBe("unknown");
    expect(result.banner.state).toBe("idle");
  });
});

describe("desk-side facts (health file: robot mode, FCI)", () => {
  test("hardware active: ok, with the mode and FCI", () => {
    const c = card("desk", setupStatus());
    expect(c.state).toBe("ok");
    expect(factText(c, "Robot mode")).toEqual(["Idle (1)"]);
    expect(factText(c, "marvel0.fact.fci")).toEqual([
      "marvel0.value.fci_active",
    ]);
  });

  test("hardware not active: not started (FCI is off)", () => {
    const c = card(
      "desk",
      setupStatus({ fr3_health: healthPart({ hardware_active: false }) }),
    );
    expect(c.state).toBe("not_started");
    expect(factText(c, "marvel0.fact.fci")).toEqual([
      "marvel0.value.fci_inactive",
    ]);
  });

  test("the file does not say: unknown, never ok", () => {
    for (const hardware of [null, undefined, "yes", 1]) {
      const c = card(
        "desk",
        setupStatus({ fr3_health: healthPart({ hardware_active: hardware }) }),
      );
      expect(c.state).toBe("unknown");
    }
  });

  test("red light: red, with the reasons as written", () => {
    const c = card(
      "desk",
      setupStatus({
        fr3_health: healthPart({
          state: "red",
          robot_mode: 4,
          robot_mode_name: "Reflex",
          reasons: ["cartesian reflex"],
          current_errors: ["communication_constraints_violation"],
        }),
      }),
    );
    expect(c.state).toBe("red");
    expect(c.notes).toEqual([
      "cartesian reflex",
      "communication_constraints_violation",
    ]);
    expect(factText(c, "Robot mode")).toEqual(["Reflex (4)"]);
  });

  test("a request deadline plus the poll gap fits inside the freshness window", () => {
    // Otherwise an answer could stay on screen as current after its window.
    expect(MARVEL0_POLL_MS + MARVEL0_REQUEST_MS).toBeLessThanOrEqual(
      MARVEL0_FRESH_MS,
    );
  });

  test("the monitor stopped updating: stale, which is not red", () => {
    const c = card(
      "desk",
      setupStatus({
        fr3_health: healthPart({
          state: "offline",
          detail: "the monitor stopped updating",
          age_s: 42,
        }),
      }),
    );
    expect(c.state).toBe("stale");
    expect(factText(c, "marvel0.fact.last_update")).toEqual(["ago:42"]);
  });

  test("the monitor stopped updating or the file cannot be read: the server's reason is a note, ahead of the last reasons", () => {
    for (const id of ["desk", "arm", "monitor"] as const) {
      const stopped = card(
        id,
        setupStatus({
          fr3_health: healthPart({
            state: "offline",
            detail: "the monitor stopped updating",
            reasons: ["cartesian reflex"],
          }),
        }),
      );
      expect(stopped.state).toBe("stale");
      // The monitor's own card lists its last reasons; the cards that mirror
      // it say so instead of repeating them.
      expect(stopped.notes).toEqual(
        id === "monitor"
          ? ["the monitor stopped updating", "cartesian reflex"]
          : [
              "the monitor stopped updating",
              { key: "marvel0.note.same_as_monitor" },
            ],
      );
      const unreadable = card(
        id,
        setupStatus({
          fr3_health: { state: "offline", detail: "health file unreadable" },
        }),
      );
      expect(unreadable.state).toBe("stale");
      expect(unreadable.notes).toEqual(["health file unreadable"]);
    }
    // Without a detail there is nothing to add.
    expect(
      card(
        "desk",
        setupStatus({ fr3_health: healthPart({ state: "offline" }) }),
      ).notes,
    ).toEqual([]);
  });

  test("no health file or no readable part: unknown, with the server's reason as a note", () => {
    expect(
      card("desk", setupStatus({ fr3_health: { state: "missing" } })).state,
    ).toBe("unknown");
    const noConfig = card(
      "desk",
      setupStatus({
        fr3_health: {
          state: "unknown",
          detail: "no health file configured (fr3.health_file)",
        },
      }),
    );
    expect(noConfig.state).toBe("unknown");
    // The summary is the catalogue's; the server's sentence is a note.
    expect(summary(noConfig)).toBe("marvel0.health.unreadable");
    expect(noConfig.notes).toEqual([
      "no health file configured (fr3.health_file)",
    ]);
    const gone = setupStatus();
    delete gone.fr3_health;
    expect(card("desk", gone).state).toBe("unknown");
  });
});

describe("terminal A, the arm stack (health file: hardware and controller)", () => {
  test("both active: ok", () => {
    expect(card("arm", setupStatus()).state).toBe("ok");
  });

  test("hardware not active: not started", () => {
    const c = card(
      "arm",
      setupStatus({
        fr3_health: healthPart({
          hardware_active: false,
          controller_active: false,
        }),
      }),
    );
    expect(c.state).toBe("not_started");
    expect(summary(c)).toBe("marvel0.arm.hardware_inactive");
  });

  test("hardware active but controller not: not started, and says which", () => {
    const c = card(
      "arm",
      setupStatus({ fr3_health: healthPart({ controller_active: false }) }),
    );
    expect(c.state).toBe("not_started");
    expect(summary(c)).toBe("marvel0.arm.controller_inactive");
  });

  test("missing fields: unknown", () => {
    for (const over of [
      { controller_active: null },
      { hardware_active: null, controller_active: true },
      { hardware_active: "true", controller_active: "true" },
    ])
      expect(
        card("arm", setupStatus({ fr3_health: healthPart(over) })).state,
      ).toBe("unknown");
  });

  test("red and offline keep their own states", () => {
    expect(
      card("arm", setupStatus({ fr3_health: healthPart({ state: "red" }) }))
        .state,
    ).toBe("red");
    expect(
      card("arm", setupStatus({ fr3_health: healthPart({ state: "offline" }) }))
        .state,
    ).toBe("stale");
  });

  test("without a health file the stack is unknown, not not-started", () => {
    const c = card("arm", setupStatus({ fr3_health: { state: "missing" } }));
    expect(c.state).toBe("unknown");
    expect(summary(c)).toBe("marvel0.health.missing_for_others");
  });

  test("the controller marker is shown with its age and never changes the state", () => {
    const old = card(
      "arm",
      setupStatus(),
      marvel0Status({ marker: { state: "present", age_s: 20 * 3600 } }),
    );
    expect(old.state).toBe("ok");
    expect(factText(old, "marvel0.fact.marker")[0]).toBe(
      "marvel0.marker.present | ago:72000 | marvel0.marker.old",
    );
    // 12 h is old, as for the server (it decides on "12 h or more").
    for (const [age, flagged] of [
      [12 * 3600 - 1, false],
      [12 * 3600, true],
    ] as const)
      expect(
        factText(
          card(
            "arm",
            setupStatus(),
            marvel0Status({ marker: { state: "present", age_s: age } }),
          ),
          "marvel0.fact.marker",
        )[0].includes("marvel0.marker.old"),
      ).toBe(flagged);
    const fresh = card("arm", setupStatus());
    expect(factText(fresh, "marvel0.fact.marker")).toEqual([
      "marvel0.marker.present | ago:95",
    ]);
    const absent = card(
      "arm",
      setupStatus(),
      marvel0Status({ marker: { state: "absent", age_s: null } }),
    );
    expect(factText(absent, "marvel0.fact.marker")).toEqual([
      "marvel0.marker.absent",
    ]);
    const unknown = card(
      "arm",
      setupStatus(),
      marvel0Status({
        marker: { state: "unknown", detail: "not a regular file" },
      }),
    );
    expect(factText(unknown, "marvel0.fact.marker")).toEqual([
      "marvel0.value.unknown | marvel0.marker.unreadable",
    ]);
    expect(unknown.notes).toEqual(["not a regular file"]);
    const none = card("arm", setupStatus(), null);
    expect(factText(none, "marvel0.fact.marker")).toEqual([
      "marvel0.marker.no_answer",
    ]);
    expect(none.state).toBe("ok");
  });
});

describe("the reasons of a health file that stopped updating are listed once", () => {
  const SAME = { key: "marvel0.note.same_as_monitor" };
  const REASONS = [
    "controller_manager_unreachable 43s",
    "robot_state_no_data 44s",
  ];
  const health = (over: Record<string, unknown> = {}) =>
    setupStatus({
      fr3_health: healthPart({
        state: "offline",
        detail: "the monitor stopped updating",
        reasons: REASONS,
        hardware_active: false,
        controller_active: false,
        ...over,
      }),
    });

  test("the monitor's card keeps them, the Desk and terminal A cards point to it", () => {
    const found = cards(view(health()));
    expect(found.monitor.notes).toEqual([
      "the monitor stopped updating",
      ...REASONS,
    ]);
    for (const id of ["desk", "arm"] as const) {
      expect(found[id].state).toBe("stale");
      // The server's sentence stays; the reason lines are replaced by one note.
      expect(found[id].notes).toEqual(["the monitor stopped updating", SAME]);
      expect(serverNotes(found[id])).not.toContain(REASONS[0]);
    }
  });

  test("the reasons are not lost: they are on exactly one of the three cards", () => {
    const found = cards(view(health()));
    const listing = ["desk", "arm", "monitor"].filter((id) =>
      serverNotes(found[id as Marvel0CardId]).includes(REASONS[0]),
    );
    expect(listing).toEqual(["monitor"]);
  });

  test("current errors the file lists count as reasons: they go the same way", () => {
    const found = cards(
      view(health({ reasons: [], current_errors: ["reflex"] })),
    );
    expect(serverNotes(found.monitor)).toContain("reflex");
    for (const id of ["desk", "arm"] as const)
      expect(found[id].notes).toEqual(["the monitor stopped updating", SAME]);
  });

  test("without any reason there is nothing to point to", () => {
    const found = cards(view(health({ reasons: [] })));
    for (const id of ["desk", "arm"] as const)
      expect(found[id].notes).toEqual(["the monitor stopped updating"]);
    expect(found.monitor.notes).toEqual(["the monitor stopped updating"]);
  });

  test("terminal A keeps its own marker note after the pointer", () => {
    const marvel0 = marvel0Status({
      marker: { state: "unknown", detail: "SERVER marker sentence" },
      robot_idle: { state: "unknown", reasons: [] },
    });
    expect(card("arm", health(), marvel0).notes).toEqual([
      "the monitor stopped updating",
      SAME,
      "SERVER marker sentence",
    ]);
    expect(card("desk", health(), marvel0).notes).toEqual([
      "the monitor stopped updating",
      SAME,
    ]);
  });

  test("a red light is a real light: all three cards list the reasons as written", () => {
    const found = cards(
      view(health({ state: "red", detail: undefined, hardware_active: true })),
    );
    for (const id of ["desk", "arm", "monitor"] as const) {
      expect(found[id].state).toBe("red");
      expect(found[id].notes).toEqual(REASONS);
    }
    // Also when the red light only follows from the stack not running.
    const down = cards(view(health({ state: "red", detail: undefined })));
    expect(down.arm.state).toBe("not_started");
    for (const id of ["desk", "arm", "monitor"] as const)
      expect(down[id].notes).toEqual(REASONS);
  });

  test("an old sample keeps what the cards say", () => {
    const found = cards(view({ ...health(), age_s: 30 }));
    expect(found.monitor.notes).toContain(REASONS[0]);
    for (const id of ["desk", "arm"] as const)
      expect(found[id].notes).toEqual(["the monitor stopped updating", SAME]);
  });

  test("the note has words in both languages", () => {
    expect(en["marvel0.note.same_as_monitor"]).toBe(
      "Same reasons as the Terminal M card.",
    );
    expect(zh["marvel0.note.same_as_monitor"]).toBe("原因同终端 M 的卡片。");
  });
});

describe("terminal A when the red light only follows from the stack not running", () => {
  const red = (reasons: unknown[], over: Record<string, unknown> = {}) =>
    setupStatus({
      fr3_health: healthPart({
        state: "red",
        hardware_active: false,
        controller_active: false,
        reasons,
        ...over,
      }),
    });
  const STACK_DOWN = [
    "hardware_interface=unconfigured",
    "controller_manager_unreachable 5769s",
    "robot_state_no_data",
  ];

  test("a monitor that waits for the first robot state is the stack not running", () => {
    // It runs, but no robot state has reached it: the arm stack is not up.
    const found = cards(view(red(["waiting_for_first_robot_state"])));
    expect(found.arm.state).toBe("not_started");
    expect(summary(found.arm)).toBe("marvel0.arm.red_follows");
    expect(found.arm.hint).toBe("marvel0.arm.hint_red_follows");
    expect(found.arm.notes).toEqual(["waiting_for_first_robot_state"]);
    // Alone or among the other stack-down reasons.
    expect(
      card("arm", red([...STACK_DOWN, "waiting_for_first_robot_state"])).state,
    ).toBe("not_started");
    // Not a Desk fault and not a monitor fault (2026-10-27: Desk was unlocked
    // with FCI on, and both cards showed the red light): with the hardware
    // interface down and the controller manager answering, Desk usually lacks
    // unlocked joints or FCI; the monitor itself works.
    expect(found.desk.state).toBe("not_started");
    expect(summary(found.desk)).toBe("marvel0.desk.inactive");
    expect(found.monitor.state).toBe("ok");
    expect(summary(found.monitor)).toBe("marvel0.monitor.ok_red_follows");
  });

  test("a reason that is not stack-down next to it keeps the light red", () => {
    expect(
      card("arm", red(["waiting_for_first_robot_state", "cartesian reflex"]))
        .state,
    ).toBe("red");
    expect(
      card("arm", red(["waiting_for_first_robot_state_extra"])).state,
    ).toBe("red");
  });

  test("with the hardware active it is a fault of the arm or its link", () => {
    expect(
      card(
        "arm",
        red(["waiting_for_first_robot_state"], { hardware_active: true }),
      ).state,
    ).toBe("red");
  });

  test("only stack-down reasons: not started, with a hint that says why", () => {
    const setup = red(STACK_DOWN);
    const found = cards(view(setup));
    expect(found.arm.state).toBe("not_started");
    expect(summary(found.arm)).toBe("marvel0.arm.red_follows");
    expect(found.arm.hint).toBe("marvel0.arm.hint_red_follows");
    // The reasons stay visible as written.
    expect(found.arm.notes).toEqual(STACK_DOWN);
    // Terminal A is not running (the controller manager does not answer): the
    // monitor cannot see Desk at all, so Desk is unknown, not a fault, and the
    // monitor itself works.
    expect(found.desk.state).toBe("unknown");
    expect(summary(found.desk)).toBe("marvel0.desk.needs_arm");
    expect(found.desk.hint).toBe("marvel0.desk.hint");
    expect(found.monitor.state).toBe("ok");
    expect(summary(found.monitor)).toBe("marvel0.monitor.ok_red_follows");
  });

  test("any other red light stays red on the Desk and monitor cards too", () => {
    for (const setup of [
      red([...STACK_DOWN, "cartesian reflex"]),
      red(STACK_DOWN, { current_errors: ["joint_reflex"] }),
      red(["robot_state_no_data"], { hardware_active: true }),
    ]) {
      const found = cards(view(setup));
      expect(found.desk.state).toBe("red");
      expect(found.monitor.state).toBe("red");
    }
  });

  test("each kind alone is enough, with what the monitor appends to it", () => {
    for (const reason of [
      "hardware_interface_missing",
      "hardware_interface_missing (no Franka hardware component loaded)",
      "hardware_interface_missing [loaded: none]",
      "hardware_interface=inactive",
      "hardware_interface=unconfigured",
      "controller_manager_unreachable",
      "controller_manager_unreachable 12s",
      "controller_manager_unreachable 3.5s",
      "robot_state_no_data",
      "robot_state_no_data 7s",
      "waiting_for_first_robot_state",
      "waiting_for_first_robot_state 3s",
      "waiting_for_first_robot_state (the arm stack is not up)",
    ])
      expect(card("arm", red([reason])).state).toBe("not_started");
  });

  test("a reason that only starts like one of them is not stack-down", () => {
    for (const reason of [
      "hardware_interface_something_new",
      "hardware_interface",
      "hardware_interface_missing_again",
      "controller_manager_unreachable_again 3s",
      "controller_manager_unreachables",
      "robot_state_no_data_yet",
      "robot_state_no_data7s",
      "waiting_for_first_robot_state_extra",
      "waiting_for_first_robot_state7s",
      "waiting_for_first_robot_states",
      "waiting_for_first_robot",
    ]) {
      expect(card("arm", red([reason])).state).toBe("red");
      // One of those among real stack-down reasons is enough to keep it red.
      expect(card("arm", red([...STACK_DOWN, reason])).state).toBe("red");
    }
  });

  test("any other reason, a robot error or no reason keeps the light red", () => {
    for (const setup of [
      red([...STACK_DOWN, "cartesian reflex"]),
      red(["cartesian reflex"]),
      red(["the robot_state_no_data kind, not at the start"]),
      red(STACK_DOWN, {
        current_errors: ["communication_constraints_violation"],
      }),
      red([]),
      red(STACK_DOWN, { reasons: undefined }),
      red(STACK_DOWN, { reasons: "hardware_interface=unconfigured" }),
    ])
      expect(card("arm", setup).state).toBe("red");
  });

  test("a stack that is up keeps the light red", () => {
    const c = card(
      "arm",
      red(STACK_DOWN, { hardware_active: true, controller_active: true }),
    );
    expect(c.state).toBe("red");
    expect(c.hint).toBe("marvel0.arm.hint");
  });

  test("a stack that does not say whether it is up is still not started", () => {
    const c = card(
      "arm",
      red(STACK_DOWN, { hardware_active: null, controller_active: null }),
    );
    expect(c.state).toBe("not_started");
  });

  test("hardware that is up keeps the light red, whatever the controller says", () => {
    // The hardware interface is loaded and active, so "no robot state" is a
    // fault of the arm or its link, not of a stack that was never started.
    for (const controller of [null, false, true]) {
      const c = card(
        "arm",
        red(["robot_state_no_data"], {
          hardware_active: true,
          controller_active: controller,
        }),
      );
      expect(c.state).toBe("red");
      expect(c.hint).toBe("marvel0.arm.hint");
    }
    // Hardware down or unknown is still the stack not running.
    for (const hardware of [false, null])
      expect(
        card(
          "arm",
          red(["robot_state_no_data"], {
            hardware_active: hardware,
            controller_active: null,
          }),
        ).state,
      ).toBe("not_started");
  });

  test("an old file and a missing one are not turned into anything else", () => {
    expect(
      card(
        "arm",
        setupStatus({
          fr3_health: healthPart({
            state: "offline",
            reasons: STACK_DOWN,
            hardware_active: false,
          }),
        }),
      ).state,
    ).toBe("stale");
    expect(
      card("arm", setupStatus({ fr3_health: { state: "missing" } })).state,
    ).toBe("unknown");
  });
});

describe("the monitor's own red_cause (fr3_health_monitor.py since 2026-10-27)", () => {
  // A newer monitor says why the light is red; the field wins over the
  // reasons, which an older monitor leaves the cards to read.
  const red = (red_cause: unknown, over: Record<string, unknown> = {}) =>
    setupStatus({
      fr3_health: healthPart({
        state: "red",
        hardware_active: null,
        controller_active: null,
        reasons: ["controller_manager_unreachable 60s", "robot_state_no_data"],
        red_cause,
        stack: red_cause === "stack_down" ? "down" : "up",
        ...over,
      }),
    });

  test("stack_down: Desk unknown until A runs, A not started, the monitor fine", () => {
    const found = cards(view(red("stack_down")));
    expect(found.desk.state).toBe("unknown");
    expect(summary(found.desk)).toBe("marvel0.desk.needs_arm");
    expect(found.arm.state).toBe("not_started");
    expect(summary(found.arm)).toBe("marvel0.arm.red_follows");
    expect(found.monitor.state).toBe("ok");
    expect(summary(found.monitor)).toBe("marvel0.monitor.ok_red_follows");
  });

  test("hardware_inactive: Desk usually lacks unlocked joints or FCI", () => {
    const found = cards(
      view(
        red("hardware_inactive", {
          hardware_active: false,
          reasons: ["hardware_interface=inactive", "robot_state_no_data"],
        }),
      ),
    );
    expect(found.desk.state).toBe("not_started");
    expect(summary(found.desk)).toBe("marvel0.desk.inactive");
    expect(found.arm.state).toBe("not_started");
    expect(found.monitor.state).toBe("ok");
  });

  test("a fault the monitor names stays red, whatever the reasons look like", () => {
    for (const cause of [
      "robot_fault",
      "hardware_missing",
      "no_robot_state",
      "a_cause_this_page_does_not_know",
    ]) {
      const found = cards(view(red(cause)));
      expect([found.desk.state, found.arm.state, found.monitor.state]).toEqual([
        "red",
        "red",
        "red",
      ]);
    }
  });

  test("a light that is not red is not changed by a stray red_cause", () => {
    const found = cards(
      view(
        setupStatus({
          fr3_health: healthPart({ state: "ok", red_cause: "stack_down" }),
        }),
      ),
    );
    expect(found.monitor.state).toBe("ok");
    expect(summary(found.monitor)).toBe("marvel0.monitor.ok");
  });

  test("the new sentences have words in both languages", () => {
    for (const key of [
      "marvel0.desk.needs_arm",
      "marvel0.desk.inactive",
      "marvel0.monitor.ok_red_follows",
    ]) {
      expect((en as Record<string, string>)[key]).toBeTruthy();
      expect((zh as Record<string, string>)[key]).toBeTruthy();
    }
    expect(zh["marvel0.desk.needs_arm"]).toContain("Desk");
    expect(en["marvel0.desk.inactive"]).toContain("FCI");
  });
});

describe("terminal B, the gripper stack", () => {
  test("has no passive source: unknown with the reason, whatever else is up", () => {
    for (const [setup, marvel0] of [
      [setupStatus(), marvel0Status()],
      [quietSetup(), quietMarvel0()],
      [null, null],
      ["junk", 7],
    ] as Array<[unknown, unknown]>) {
      const c = card("gripper", setup, marvel0);
      expect(c.state).toBe("unknown");
      expect(summary(c)).toBe("marvel0.gripper.reason");
      expect(c.facts).toEqual([]);
    }
  });
});

describe("terminal C, the robot server (port 5000 listening only)", () => {
  test("listening: ok; the owner's name is shown when known", () => {
    const c = card("robot_server", setupStatus());
    expect(c.state).toBe("ok");
    expect(factText(c, "marvel0.fact.port")).toEqual([
      "marvel0.port.listening | python",
    ]);
  });

  test("not listening: not started", () => {
    const c = card("robot_server", setupStatus({ ports: portsPart([8000]) }));
    expect(c.state).toBe("not_started");
    expect(factText(c, "marvel0.fact.port")).toEqual(["marvel0.port.closed"]);
  });

  test("the table cannot be read, lacks the port or is malformed: unknown", () => {
    const unreadable = card(
      "robot_server",
      setupStatus({
        ports: { state: "unknown", detail: "/proc/net/tcp cannot be read" },
      }),
    );
    expect(unreadable.state).toBe("unknown");
    expect(summary(unreadable)).toBe("marvel0.port.no_table");
    expect(unreadable.notes).toEqual(["/proc/net/tcp cannot be read"]);
    const notWatched = card(
      "robot_server",
      setupStatus({ ports: portsPart([], [5000]) }),
    );
    expect(notWatched.state).toBe("unknown");
    expect(factText(notWatched, "marvel0.fact.port")).toEqual([
      "marvel0.port.not_watched",
    ]);
    expect(
      card(
        "robot_server",
        setupStatus({
          ports: { state: "ok", ports: [{ port: 5000, listening: "yes" }] },
        }),
      ).state,
    ).toBe("unknown");
    expect(
      card("robot_server", setupStatus({ ports: { state: "ok", ports: "x" } }))
        .state,
    ).toBe("unknown");
  });
});

describe("the health monitor (M): stale is not red", () => {
  const monitor = (fr3_health: unknown) =>
    card("monitor", setupStatus({ fr3_health }));

  test.each([
    ["ok", "ok"],
    ["red", "red"],
    ["offline", "stale"],
    ["missing", "not_started"],
    ["unknown", "unknown"],
    ["something-new", "unknown"],
  ] as Array<[string, Marvel0State]>)("health %s is %s", (health, expected) => {
    expect(monitor(healthPart({ state: health })).state).toBe(expected);
  });

  test("a part that is not an object is unknown", () => {
    for (const junk of [null, "ok", 3, [], { state: 5 }])
      expect(monitor(junk).state).toBe("unknown");
  });

  test("shows the age and the command success rate", () => {
    const c = monitor(healthPart({ age_s: 0.5, command_success_rate: 0.987 }));
    expect(factText(c, "marvel0.fact.last_update")).toEqual(["ago:0.5"]);
    expect(factText(c, "marvel0.fact.success_rate")).toEqual(["99 %"]);
    expect(
      monitor(healthPart({ command_success_rate: "high" })).facts,
    ).toHaveLength(1);
  });
});

describe("the policy server (port 8000 and levi-aeri-policy-* units)", () => {
  const units = (...items: Array<[string, string, string]>) =>
    marvel0Status({
      units: {
        state: "ok",
        items: items.map(([name, active, sub]) => ({ name, active, sub })),
      },
    });

  test("listening: ok, and the unit is listed", () => {
    const c = card("policy", setupStatus());
    expect(c.state).toBe("ok");
    expect(factText(c, "marvel0.fact.unit")).toEqual([
      "levi-aeri-policy-pi05.service: active (running)",
    ]);
  });

  test("not listening and no unit: not started", () => {
    const c = card("policy", setupStatus({ ports: portsPart([]) }), units());
    expect(c.state).toBe("not_started");
    expect(summary(c)).toContain("marvel0.policy.closed");
    expect(factText(c, "marvel0.fact.units")).toEqual(["marvel0.units.none"]);
  });

  test("a unit that runs while the port is closed is still not started (loading)", () => {
    for (const active of ["active", "activating"]) {
      const c = card(
        "policy",
        setupStatus({ ports: portsPart([]) }),
        units(["levi-aeri-policy-a.service", active, "start"]),
      );
      expect(c.state).toBe("not_started");
      expect(summary(c)).toContain("marvel0.policy.loading");
    }
  });

  test("a failed unit with the port closed is red; with the port open it is ok", () => {
    const failed = units(["levi-aeri-policy-a.service", "failed", "failed"]);
    expect(
      card("policy", setupStatus({ ports: portsPart([]) }), failed).state,
    ).toBe("red");
    expect(card("policy", setupStatus(), failed).state).toBe("ok");
  });

  test("only policy units count, not the run units", () => {
    const c = card(
      "policy",
      setupStatus({ ports: portsPart([]) }),
      units(["levi-aeri-run-x.service", "failed", "failed"]),
    );
    expect(c.state).toBe("not_started");
    expect(factText(c, "marvel0.fact.units")).toEqual(["marvel0.units.none"]);
  });

  test("the port table cannot be read: unknown, even with a unit running", () => {
    const c = card(
      "policy",
      setupStatus({ ports: { state: "unknown", detail: "no /proc" } }),
    );
    expect(c.state).toBe("unknown");
    expect(summary(c)).toBe("marvel0.port.no_table");
    expect(c.notes).toEqual(["no /proc"]);
  });

  test("units unknown: the port decides, the fact says unknown", () => {
    const c = card(
      "policy",
      setupStatus(),
      marvel0Status({
        units: { state: "unknown", detail: "systemctl not found" },
      }),
    );
    expect(c.state).toBe("ok");
    expect(factText(c, "marvel0.fact.units")).toEqual([
      "marvel0.value.unknown | marvel0.units.no_answer",
    ]);
    expect(c.notes).toEqual(["systemctl not found"]);
    const closed = card("policy", setupStatus({ ports: portsPart([]) }), null);
    expect(closed.state).toBe("not_started");
  });

  test("malformed unit entries are skipped", () => {
    const c = card(
      "policy",
      setupStatus(),
      marvel0Status({
        units: { state: "ok", items: [null, 3, { name: 4 }, { active: "x" }] },
      }),
    );
    expect(c.state).toBe("ok");
    expect(factText(c, "marvel0.fact.units")).toEqual(["marvel0.units.none"]);
  });

  test("a unit that does not say its state is shown with the catalogue's word", () => {
    const c = card(
      "policy",
      setupStatus({ ports: portsPart([]) }),
      marvel0Status({
        units: {
          state: "ok",
          items: [
            { name: "levi-aeri-policy-a.service" },
            { name: "levi-aeri-policy-b.service", active: "active" },
          ],
        },
      }),
    );
    expect(factText(c, "marvel0.fact.unit")).toEqual([
      "levi-aeri-policy-a.service | marvel0.value.unknown",
      "levi-aeri-policy-b.service: active",
    ]);
    // Only the second one runs.
    expect(summary(c)).toContain("marvel0.policy.loading");
  });
});

describe("the demo client (recent evaluation sessions)", () => {
  const sessions = (...items: unknown[]) =>
    marvel0Status({
      eval_sessions: { state: "ok", roots: 2, sessions: items },
    });
  const demo = (marvel0: unknown) => card("demo", setupStatus(), marvel0);

  test(`a running session within ${RECENT_S} s: ok`, () => {
    const c = demo(
      sessions({ group: "pi05", age_s: RECENT_S, status: "running" }),
    );
    expect(c.state).toBe("ok");
    expect(factText(c, "marvel0.fact.roots")).toEqual(["2"]);
  });

  test("a session with no state that keeps updating counts as running", () => {
    expect(demo(sessions({ group: "g", age_s: 3, status: null })).state).toBe(
      "ok",
    );
  });

  test("a recent fault or crash: red, ahead of a healthy session", () => {
    for (const status of ["fault", "crashed"])
      expect(
        demo(
          sessions(
            { group: "a", age_s: 5, status: "running" },
            { group: "b", age_s: 20, status },
          ),
        ).state,
      ).toBe("red");
  });

  test("a session that has not ended and went quiet for a while: stale", () => {
    for (const age of [RECENT_S + 1, 300, 3000]) {
      const c = demo(sessions({ group: "a", age_s: age, status: "running" }));
      expect(c.state).toBe("stale");
      expect(summary(c)).toContain("marvel0.demo.silent");
    }
  });

  test("a fault older than a minute stays red and still says that a fault happened", () => {
    for (const status of ["fault", "crashed"])
      for (const age of [RECENT_S + 1, 300, 590]) {
        const c = demo(sessions({ group: "a", age_s: age, status }));
        expect(c.state).toBe("red");
        expect(c.summary).toEqual({
          key: "marvel0.demo.fault_earlier",
          params: { age: { ago: age } },
        });
      }
    // The latest fault names its age, and a session gone quiet does not hide it.
    const c = demo(
      sessions(
        { group: "a", age_s: 500, status: "fault" },
        { group: "b", age_s: 200, status: "running" },
        { group: "c", age_s: 90, status: "crashed" },
      ),
    );
    expect(c.state).toBe("red");
    expect(c.summary).toEqual({
      key: "marvel0.demo.fault_earlier",
      params: { age: { ago: 90 } },
    });
    // The sessions stay listed with their states and ages.
    expect(factText(c, "c")).toEqual(["crashed | ago:90"]);
  });

  test("a fault within the minute keeps its own headline, and a session that updates now outranks an older fault", () => {
    const fresh = demo(sessions({ group: "a", age_s: 30, status: "fault" }));
    expect(fresh.state).toBe("red");
    expect(summary(fresh)).toContain("marvel0.demo.fault ");
    const running = demo(
      sessions(
        { group: "a", age_s: 5, status: "running" },
        { group: "b", age_s: 200, status: "fault" },
      ),
    );
    expect(running.state).toBe("ok");
    // The fault is still listed.
    expect(factText(running, "b")).toEqual(["fault | ago:200"]);
  });

  test("an older fault is red even when the list is incomplete", () => {
    const c = demo(
      marvel0Status({
        eval_sessions: {
          state: "unknown",
          roots: 2,
          sessions: [{ group: "a", age_s: 200, status: "fault" }],
          detail: "1 session file(s) could not be read",
        },
      }),
    );
    expect(c.state).toBe("red");
    expect(flat(c.summary)).toContain("marvel0.demo.fault_earlier");
  });

  test("sessions that ended normally, or none: not started", () => {
    const ended = demo(
      sessions(
        { group: "a", age_s: 5, status: "finished" },
        { group: "b", age_s: 90, status: "stopped" },
      ),
    );
    expect(ended.state).toBe("not_started");
    expect(summary(ended)).toContain("marvel0.demo.ended");
    const none = demo(sessions());
    expect(none.state).toBe("not_started");
    expect(summary(none)).toContain("marvel0.demo.none");
  });

  test("sessions are listed newest first, at most six, malformed ones skipped", () => {
    const many = Array.from({ length: 9 }, (_, i) => ({
      group: `g${i}`,
      age_s: 9 - i,
      status: "running",
    }));
    const c = demo(
      sessions(
        null,
        "x",
        { group: "", age_s: 1 },
        { group: "n", age_s: "1" },
        ...many,
      ),
    );
    const groups = c.facts
      .filter((f) => "raw" in f.label)
      .map((f) => flat(f.label));
    expect(groups).toEqual(["g8", "g7", "g6", "g5", "g4", "g3"]);
  });

  test("no answer, or a part that is not ok: unknown, with the server's reason as a note", () => {
    expect(demo(null).state).toBe("unknown");
    const c = demo(
      marvel0Status({
        eval_sessions: { state: "unknown", detail: "no rollout roots" },
      }),
    );
    expect(c.state).toBe("unknown");
    expect(summary(c)).toBe("marvel0.demo.no_answer");
    expect(c.notes).toEqual(["no rollout roots"]);
    expect(
      demo(marvel0Status({ eval_sessions: { state: "ok", sessions: "x" } }))
        .state,
    ).toBe("not_started");
  });

  describe("when the part is not ok but found sessions", () => {
    const partial = (detail: string, ...items: unknown[]) =>
      marvel0Status({
        eval_sessions: { state: "unknown", roots: 2, sessions: items, detail },
      });
    const DETAIL = "1 session file(s) could not be read";

    test("a recent running session still counts, with the detail as a note", () => {
      const c = demo(
        partial(DETAIL, { group: "pi05", age_s: 3, status: "running" }),
      );
      expect(c.state).toBe("ok");
      expect(summary(c)).toContain("marvel0.demo.ok");
      expect(c.notes).toEqual([DETAIL]);
      expect(c.facts.map((f) => flat(f.label))).toContain("pi05");
    });

    test("a recent fault is red", () => {
      const c = demo(
        partial(DETAIL, { group: "pi05", age_s: 3, status: "fault" }),
      );
      expect(c.state).toBe("red");
      expect(c.notes).toEqual([DETAIL]);
    });

    test("sessions that look ended do not prove that nothing runs: unknown", () => {
      const c = demo(
        partial(DETAIL, { group: "pi05", age_s: 3, status: "finished" }),
      );
      expect(c.state).toBe("unknown");
      expect(summary(c)).toContain("marvel0.demo.partial");
      expect(c.notes).toEqual([DETAIL]);
      expect(c.facts.map((f) => flat(f.label))).toContain("pi05");
    });

    test("no session found: unknown, with the server's reason as a note", () => {
      const c = demo(partial(DETAIL));
      expect(c.state).toBe("unknown");
      expect(summary(c)).toBe("marvel0.demo.no_answer");
      expect(c.notes).toEqual([DETAIL]);
    });
  });

  test("the detail of an ok part (a capped list) is a note too", () => {
    const c = demo(
      marvel0Status({
        eval_sessions: {
          state: "ok",
          roots: 1,
          sessions: [{ group: "a", age_s: 1, status: "running" }],
          detail: "only the 50 most recent sessions are listed",
        },
      }),
    );
    expect(c.state).toBe("ok");
    expect(c.notes).toEqual(["only the 50 most recent sessions are listed"]);
  });

  test("a session with no state is shown with the catalogue's word, not a literal", () => {
    const c = demo(sessions({ group: "g", age_s: 3 }));
    expect(factText(c, "g")).toEqual(["marvel0.value.unknown | ago:3"]);
  });
});

describe("the live service", () => {
  test("alive: ok, with state, age, pause and gate", () => {
    const c = card("live", setupStatus());
    expect(c.state).toBe("ok");
    expect(factText(c, "marvel0.fact.service")).toEqual(["idle"]);
    expect(factText(c, "marvel0.fact.labelling_paused")).toEqual([
      "marvel0.value.no",
    ]);
    expect(factText(c, "marvel0.fact.gpu_gate")).toEqual([
      "marvel0.value.open",
    ]);
    expect(factText(c, "marvel0.fact.port")).toHaveLength(2);
  });

  test("the gate's own reason text is passed on as a note", () => {
    const c = card(
      "live",
      setupStatus({
        live: livePart({
          gate: { open: false, code: "robot_quiet", reason: "robot busy" },
        }),
      }),
    );
    expect(c.notes).toEqual(["robot busy"]);
    expect(factText(c, "marvel0.fact.gpu_gate")).toEqual([
      "marvel0.value.closed | robot_quiet",
    ]);
  });

  test("status file old: stale; the service says it stopped: not started", () => {
    expect(
      card("live", setupStatus({ live: livePart({ alive: false, age_s: 90 }) }))
        .state,
    ).toBe("stale");
    expect(
      card(
        "live",
        setupStatus({
          live: livePart({ alive: false, service_state: "stopped" }),
        }),
      ).state,
    ).toBe("not_started");
  });

  test("no status file: the core port closed means not started; otherwise unknown", () => {
    const noFile = {
      state: "unknown",
      detail: "the live service has no status file",
    };
    const closed = card(
      "live",
      setupStatus({ live: noFile, ports: portsPart([8100]) }),
    );
    expect(closed.state).toBe("not_started");
    const open = card("live", setupStatus({ live: noFile }));
    expect(open.state).toBe("unknown");
    expect(summary(open)).toBe("marvel0.live.no_status");
    expect(open.notes).toEqual(["the live service has no status file"]);
    const blind = card(
      "live",
      setupStatus({ live: noFile, ports: { state: "unknown" } }),
    );
    expect(blind.state).toBe("unknown");
    const gone = setupStatus();
    delete gone.live;
    expect(card("live", gone).state).toBe("unknown");
  });
});

describe("the model server (ports 8100 and 8110)", () => {
  test("the main port listening: ok, with the model's state", () => {
    const c = card("vlm", setupStatus());
    expect(c.state).toBe("ok");
    expect(factText(c, "marvel0.fact.model_state")).toEqual(["awake"]);
  });

  test("the alternate port alone is enough", () => {
    const ports = portsPart([]);
    (ports.ports as unknown[]).push({
      port: 8110,
      role: "vllm_alt",
      listening: true,
      process: "python",
    });
    expect(card("vlm", setupStatus({ ports })).state).toBe("ok");
  });

  test("neither listening: not started, even when the alternate port is not watched", () => {
    const c = card("vlm", setupStatus({ ports: portsPart([5000]) }));
    expect(c.state).toBe("not_started");
    expect(factText(c, "marvel0.fact.port")).toEqual([
      "marvel0.port.closed",
      "marvel0.port.not_watched",
    ]);
  });

  test("the table cannot be read: unknown; the model state needs a live part that is ok", () => {
    expect(
      card("vlm", setupStatus({ ports: { state: "unknown" } })).state,
    ).toBe("unknown");
    const noLive = card("vlm", setupStatus({ live: { state: "unknown" } }));
    expect(factText(noLive, "marvel0.fact.model_state")).toEqual([]);
  });
});

describe("the GPU (used/total, apps, lock holder)", () => {
  test("ok, with memory, utilisation, programs and the lock holder", () => {
    const c = card("gpu", setupStatus());
    expect(c.state).toBe("ok");
    expect(factText(c, "GPU memory")).toEqual([
      "NVIDIA GeForce RTX 5090 · 12.0 / 32.0 GiB · 7 %",
    ]);
    expect(factText(c, "marvel0.fact.gpu_apps")).toEqual(["1"]);
    expect(factText(c, "marvel0.fact.gpu_app")).toEqual([
      "python · policy_server · 9.0 GiB · pid 4008",
    ]);
    expect(factText(c, "GPU lock")).toEqual([
      'marvel0.lock.held {"label":"levi","holder":"python (pid 4008)"}',
      'marvel0.lock.free {"label":"live"}',
    ]);
  });

  test("no program on the GPU is said so", () => {
    const c = card("gpu", setupStatus({ gpu: gpuPart({ processes: [] }) }));
    expect(factText(c, "marvel0.fact.gpu_apps")).toEqual([
      "marvel0.gpu.no_apps",
    ]);
  });

  test("lock files: absent, unreadable, or none configured", () => {
    const locks = (value: unknown) =>
      factText(card("gpu", setupStatus({ gpu_locks: value })), "GPU lock");
    expect(
      locks({
        state: "ok",
        locks: [{ label: "levi", state: "absent", holders: [] }],
      }),
    ).toEqual(['marvel0.lock.absent {"label":"levi"}']);
    expect(
      locks({
        state: "ok",
        locks: [{ label: "levi", state: "unknown", holders: [] }],
      }),
    ).toEqual(['marvel0.lock.unknown {"label":"levi"}']);
    expect(locks({ state: "ok", locks: [] })).toEqual(["marvel0.gpu.no_locks"]);
    expect(
      locks({ state: "unknown", detail: "no GPU lock file configured" }),
    ).toEqual(["marvel0.value.unknown | marvel0.gpu.no_locks"]);
    expect(
      card(
        "gpu",
        setupStatus({
          gpu_locks: {
            state: "unknown",
            detail: "no GPU lock file configured",
          },
        }),
      ).notes,
    ).toEqual(["no GPU lock file configured"]);
    expect(locks("junk")[0]).toContain("marvel0.value.unknown");
  });

  test("a lock held by someone with no readable name or pid", () => {
    const part = {
      state: "ok",
      locks: [
        { label: "live", state: "held", holders: [{ kind: "FLOCK" }, null] },
      ],
    };
    expect(
      factText(card("gpu", setupStatus({ gpu_locks: part })), "GPU lock"),
    ).toEqual(['marvel0.lock.held {"label":"live","holder":"?, ?"}']);
  });

  test("unreadable GPU: unknown, with the server's reason as a note; the lock still shows", () => {
    const c = card(
      "gpu",
      setupStatus({
        gpu: { state: "unknown", detail: "nvidia-smi cannot be read" },
      }),
    );
    expect(c.state).toBe("unknown");
    expect(summary(c)).toBe("marvel0.gpu.no_answer");
    expect(c.notes).toEqual(["nvidia-smi cannot be read"]);
    expect(factText(c, "GPU lock")).toHaveLength(2);
    expect(card("gpu", setupStatus({ gpu: gpuPart({ gpus: [] }) })).state).toBe(
      "unknown",
    );
    expect(
      card("gpu", setupStatus({ gpu: gpuPart({ gpus: [null, 4] }) })).state,
    ).toBe("unknown");
  });

  test("a reused sample is marked", () => {
    const c = card("gpu", setupStatus({ gpu: gpuPart({ reused: true }) }));
    expect(factText(c, "marvel0.fact.sample")).toEqual(["marvel0.gpu.reused"]);
  });
});

describe("the robot idle? banner", () => {
  test("passes the verdict and each reason with its source, state and the server's sentence", () => {
    const { banner } = view(
      setupStatus(),
      marvel0Status({
        robot_idle: {
          state: "busy",
          reasons: [
            {
              source: "marker",
              state: "busy",
              text: "the arm-controller marker is present (for 2 min)",
            },
            {
              source: "units",
              state: "busy",
              text: "levi-aeri-policy-pi05.service is active (running)",
            },
            {
              source: "eval_sessions",
              state: "unknown",
              text: "1 session file(s) could not be read",
            },
          ],
        },
      }),
    );
    expect(banner.state).toBe("busy");
    expect(banner.note).toBeNull();
    expect(
      banner.reasons.map((r) => [flat(r.source), flat(r.state), r.text]),
    ).toEqual([
      [
        "marvel0.source.marker",
        "marvel0.reason.busy",
        "the arm-controller marker is present (for 2 min)",
      ],
      [
        "marvel0.source.units",
        "marvel0.reason.busy",
        "levi-aeri-policy-pi05.service is active (running)",
      ],
      [
        "Evaluation sessions",
        "marvel0.reason.unknown",
        "1 session file(s) could not be read",
      ],
    ]);
  });

  test("the reasons of a real answer carry the two states a server emits", () => {
    const { banner } = view(setupStatus(), marvel0Status());
    expect(banner.reasons.length).toBeGreaterThan(0);
    for (const reason of banner.reasons)
      expect(["marvel0.reason.busy", "marvel0.reason.unknown"]).toContain(
        flat(reason.state),
      );
  });

  test("a state the page does not know is shown as written, a missing one is cannot tell", () => {
    const { banner } = view(
      setupStatus(),
      marvel0Status({
        robot_idle: {
          state: "busy",
          reasons: [
            { source: "units", state: "present", text: "a" },
            { source: "units", text: "b" },
          ],
        },
      }),
    );
    // The one that cannot tell comes before a state nobody knows.
    expect(banner.reasons.map((r) => flat(r.state))).toEqual([
      "marvel0.reason.unknown",
      "present",
    ]);
  });

  test("a source the page does not know is shown as written", () => {
    const { banner } = view(
      setupStatus(),
      marvel0Status({
        robot_idle: {
          state: "busy",
          reasons: [
            {
              source: "guard",
              state: "busy",
              text: "robot-side process running: ros2_control_node",
            },
            {
              source: "eval_sessions",
              state: "busy",
              text: "evaluation session 'pi05' updated 5 s ago, running",
            },
            { source: "gripper", state: "busy", text: "y" },
          ],
        },
      }),
    );
    expect(banner.reasons.map((r) => flat(r.source))).toEqual([
      "marvel0.source.guard",
      "Evaluation sessions",
      "gripper",
    ]);
  });

  test("a source named like an Object property is shown as written, not looked up", () => {
    const sources = ["constructor", "__proto__", "toString", "hasOwnProperty"];
    const { banner } = view(
      setupStatus(),
      marvel0Status({
        robot_idle: {
          state: "busy",
          reasons: sources.map((source) => ({
            source,
            state: "busy",
            text: "t",
          })),
        },
      }),
    );
    expect(banner.reasons.map((r) => r.source)).toEqual(
      sources.map((source) => ({ raw: source })),
    );
  });

  test.each(["idle", "busy", "unknown"] as const)("the %s verdict", (state) => {
    expect(
      view(setupStatus(), marvel0Status({ robot_idle: { state, reasons: [] } }))
        .banner.state,
    ).toBe(state);
  });

  test("malformed reasons are dropped, at most twelve are kept", () => {
    const reasons = [
      null,
      "x",
      { source: "marker" },
      { source: "marker", text: "" },
      { text: "no source" },
      ...Array.from({ length: 20 }, (_, i) => ({
        source: "marker",
        state: "busy",
        text: `reason ${i}`,
      })),
    ];
    const { banner } = view(
      setupStatus(),
      marvel0Status({ robot_idle: { state: "busy", reasons } }),
    );
    expect(banner.reasons.length).toBeLessThanOrEqual(12);
    expect(banner.reasons[0].text).toBe("reason 0");
    expect(
      flat(
        view(
          setupStatus(),
          marvel0Status({
            robot_idle: {
              state: "busy",
              reasons: [{ source: "units", text: "t" }],
            },
          }),
        ).banner.reasons[0].state,
      ),
    ).toBe("marvel0.reason.unknown");
  });

  test("no marvel0 answer: cannot tell, and why", () => {
    const { banner } = marvel0View({
      setup: setupStatus(),
      marvel0: null,
      marvel0Error: "HTTP 404",
    });
    expect(banner.state).toBe("unknown");
    expect(banner.reasons).toEqual([]);
    expect(flat(banner.note!)).toBe("marvel0.banner.no_answer | HTTP 404");
    expect(flat(marvel0View({ setup: null, marvel0: null }).banner.note!)).toBe(
      "marvel0.banner.no_answer",
    );
  });

  test("an answer with no usable verdict is unknown, never idle", () => {
    for (const idle of [
      undefined,
      null,
      {},
      { state: "free" },
      { state: 1 },
      "idle",
    ]) {
      const { banner } = view(
        setupStatus(),
        marvel0Status({ robot_idle: idle }),
      );
      expect(banner.state).toBe("unknown");
      expect(flat(banner.note!)).toBe("marvel0.banner.malformed");
    }
  });
});

describe("the hint about a marker left over from the last session", () => {
  const LEFTOVER = "marvel0.hint.marker_leftover";
  const MARKER = {
    source: "marker",
    state: "busy",
    text: "the arm-controller marker is present (for 30 min)",
    code: "marker.present",
    params: { age_s: 1800 },
  };
  const markerOf = (age: unknown) => ({ ...MARKER, params: { age_s: age } });
  const OTHER = {
    guard: {
      source: "guard",
      state: "busy",
      text: "a robot-side process is running: franka_ros2",
      code: "guard.robot_process",
      params: { names: "franka_ros2" },
    },
    units: {
      source: "units",
      state: "busy",
      text: "levi-aeri-policy-pi05.service is active (running)",
      code: "units.active",
      params: {
        name: "levi-aeri-policy-pi05.service",
        active: "active",
        sub: "running",
      },
    },
    eval_sessions: {
      source: "eval_sessions",
      state: "busy",
      text: "evaluation session 'pi05_run_a' updated 12 s ago, running",
      code: "sessions.recent",
      params: { group: "pi05_run_a", age_s: 12, status: "running" },
    },
  };
  const OFFLINE = healthPart({
    state: "offline",
    detail: "the monitor stopped updating",
    reasons: ["controller_manager_unreachable 43s", "robot_state_no_data 44s"],
  });
  const MISSING = {
    state: "missing",
    detail: "no health file: the monitor is not running",
  };
  const banner = (
    health: unknown,
    reasons: unknown[],
    state: string = "busy",
  ) =>
    view(
      setupStatus({ fr3_health: health }),
      marvel0Status({ robot_idle: { state, reasons } }),
    ).banner;

  test("only a marker that is there, and the health monitor not running: one hint", () => {
    for (const health of [OFFLINE, MISSING]) {
      const found = banner(health, [MARKER]);
      expect(found.state).toBe("busy");
      expect(flat(found.hint!)).toBe(LEFTOVER);
      // The reason itself is still listed, worded as before.
      expect(found.reasons.map((r) => flat(r.source))).toEqual([
        "marvel0.source.marker",
      ]);
    }
  });

  test("a busy marker reason from an older server, which sends no code and no age, gets no hint", () => {
    const older = { source: "marker", state: "busy", text: MARKER.text };
    expect(banner(OFFLINE, [older]).hint).toBeNull();
  });

  test("a marker younger than 10 minutes gets no hint: it is the one the operator just placed before the 60 s wait", () => {
    // setup.md: touch the marker, sleep 60, then start the arm stack; for that
    // time the monitor is not running and no robot process exists yet.
    for (const age of [0, 5, 60, 95, 599.9])
      expect(banner(OFFLINE, [markerOf(age)]).hint).toBeNull();
    for (const age of [600, 601, 1800, 43000])
      expect(flat(banner(OFFLINE, [markerOf(age)]).hint!)).toBe(LEFTOVER);
    // An age the page cannot read is not an old marker.
    for (const age of [undefined, null, "1800", Number.NaN, -5])
      expect(banner(OFFLINE, [markerOf(age)]).hint).toBeNull();
    expect(banner(OFFLINE, [{ ...MARKER, params: undefined }]).hint).toBeNull();
  });

  test("the health state of an old setup sample is not trusted: no hint", () => {
    const marvel0 = marvel0Status({
      robot_idle: { state: "busy", reasons: [MARKER] },
    });
    expect(
      view(setupStatus({ fr3_health: OFFLINE }), marvel0).banner.hint,
    ).not.toBeNull();
    for (const old of [{ stale: true }, { age_s: 40 }])
      expect(
        view(setupStatus({ fr3_health: OFFLINE, ...old }), marvel0).banner.hint,
      ).toBeNull();
  });

  test("a health file that cannot be read is not a monitor that stopped: no hint", () => {
    const unreadable = { state: "offline", detail: "health file unreadable" };
    expect(banner(unreadable, [MARKER]).hint).toBeNull();
  });

  test("the health file is ok, red, unreadable or not in the answer: no hint", () => {
    const gone = setupStatus();
    delete gone.fr3_health;
    for (const setup of [
      setupStatus({ fr3_health: healthPart() }),
      setupStatus({ fr3_health: healthPart({ state: "red", reasons: ["x"] }) }),
      setupStatus({ fr3_health: { state: "unknown", detail: "x" } }),
      gone,
      null,
    ])
      expect(
        view(
          setup,
          marvel0Status({ robot_idle: { state: "busy", reasons: [MARKER] } }),
        ).banner.hint,
      ).toBeNull();
  });

  test.each(Object.keys(OTHER))(
    "a %s reason next to the marker: no hint",
    (source) => {
      const other = OTHER[source as keyof typeof OTHER];
      expect(banner(OFFLINE, [MARKER, other]).hint).toBeNull();
      expect(banner(OFFLINE, [other, MARKER]).hint).toBeNull();
      expect(banner(MISSING, [other]).hint).toBeNull();
    },
  );

  test("the real busy sample (marker, session and unit) gets no hint", () => {
    expect(
      view(setupStatus({ fr3_health: OFFLINE }), marvel0Status()).banner.hint,
    ).toBeNull();
  });

  test("a marker reason that is not 'present' gets no hint", () => {
    for (const reason of [
      { ...MARKER, state: "unknown", code: "marker.old" },
      { ...MARKER, state: "unknown", code: "marker.unreadable" },
      { ...MARKER, state: "unknown", code: "marker.no_folder" },
    ])
      expect(banner(OFFLINE, [reason], "unknown").hint).toBeNull();
    // The same with a busy verdict (the check is on the reason, not only on it).
    expect(
      banner(OFFLINE, [{ ...MARKER, code: "marker.old" }]).hint,
    ).toBeNull();
    expect(banner(OFFLINE, [{ ...MARKER, state: "unknown" }]).hint).toBeNull();
    // A code the page does not know says nothing about a leftover.
    expect(
      banner(OFFLINE, [{ ...MARKER, code: "marker.something_new" }]).hint,
    ).toBeNull();
    // Nor an entry the page cannot read, next to a good one.
    expect(banner(OFFLINE, [MARKER, null]).hint).toBeNull();
    expect(banner(OFFLINE, [MARKER, { source: "marker" }]).hint).toBeNull();
  });

  test("no reasons, or a verdict that is not 'busy': no hint", () => {
    expect(banner(OFFLINE, []).hint).toBeNull();
    expect(banner(OFFLINE, [MARKER], "unknown").hint).toBeNull();
    // An idle verdict that lists a reason is withdrawn, hint included.
    const idle = banner(OFFLINE, [MARKER], "idle");
    expect(idle.state).toBe("unknown");
    expect(idle.hint).toBeNull();
  });

  test("a banner without an answer, a malformed one and a quiet one carry no hint", () => {
    expect(view(setupStatus({ fr3_health: OFFLINE }), null).banner.hint).toBe(
      null,
    );
    expect(
      view(setupStatus({ fr3_health: OFFLINE }), { robot_idle: 3 }).banner.hint,
    ).toBeNull();
    expect(view(quietSetup(), quietMarvel0()).banner.hint).toBeNull();
  });

  test("the words exist in both languages, without a path", () => {
    expect(en["marvel0.hint.marker_leftover"]).toBe(
      "If the arm stack is down, this is probably a marker left over from the last session. Remove it as the shutdown steps in setup.md say.",
    );
    expect(zh["marvel0.hint.marker_leftover"]).toBe(
      "如果机械臂栈已经停了，这多半是上次运行结束后没删的标记。按 setup.md 里“终端 A 退出后删除标记”的那一步处理。",
    );
    for (const text of [
      en["marvel0.hint.marker_leftover"],
      zh["marvel0.hint.marker_leftover"],
    ])
      expect(text).not.toMatch(/\/(?:home|tmp|var|opt)\b|\.state|\.tmp/);
  });
});

describe("an idle verdict is kept only while the parts under it are known", () => {
  const idle = (
    change: (marvel0: Record<string, unknown>) => void = () => {},
  ) => {
    const marvel0 = quietMarvel0();
    change(marvel0);
    return view(setupStatus(), marvel0).banner;
  };

  test("all parts known and the schema as expected: idle, with no note", () => {
    const banner = idle();
    expect(banner.state).toBe("idle");
    expect(banner.note).toBeNull();
    expect(MARVEL0_SCHEMA).toBe("levi.marvel0.status.v1");
    expect(quietMarvel0().schema).toBe(MARVEL0_SCHEMA);
  });

  test("quietMarvel0 is a fresh copy of the committed quiet sample each time", () => {
    expect(quietMarvel0()).toEqual(quietSample);
    expect(quietMarvel0()).not.toBe(quietMarvel0());
    const first = quietMarvel0();
    (first.marker as Record<string, unknown>).state = "present";
    expect(quietMarvel0()).toEqual(quietSample);
    expect(quietSample.robot_idle.state).toBe("idle");
  });

  test("a schema that is not in the answer is not held against it", () => {
    expect(idle((marvel0) => delete marvel0.schema).state).toBe("idle");
  });

  test.each(["marker", "eval_sessions", "units"])(
    "%s unknown: the idle verdict becomes unknown, and says why",
    (part) => {
      const banner = idle((marvel0) => {
        marvel0[part] = {
          ...(marvel0[part] as object),
          state: "unknown",
          detail: "x",
        };
      });
      expect(banner.state).toBe("unknown");
      expect(flat(banner.note!)).toBe("marvel0.banner.idle_unverified");
    },
  );

  test.each(["marker", "eval_sessions", "units"])(
    "%s missing or malformed: not idle either",
    (part) => {
      for (const value of [undefined, null, "ok", {}, { state: 3 }]) {
        const banner = idle((marvel0) => {
          marvel0[part] = value;
        });
        expect(banner.state).toBe("unknown");
        expect(flat(banner.note!)).toBe("marvel0.banner.idle_unverified");
      }
    },
  );

  test("a present marker is a known part", () => {
    expect(
      idle((marvel0) => {
        marvel0.marker = { state: "present", age_s: 13 * 3600, detail: null };
      }).state,
    ).toBe("idle");
  });

  test("another schema, or one that is not text: unknown, and says so", () => {
    for (const schema of ["levi.marvel0.status.v2", "", null, 1]) {
      const banner = idle((marvel0) => {
        marvel0.schema = schema;
      });
      expect(banner.state).toBe("unknown");
      expect(flat(banner.note!)).toBe("marvel0.banner.idle_schema");
    }
  });

  test("busy and unknown verdicts are not changed, whatever the parts say", () => {
    for (const state of ["busy", "unknown"]) {
      const banner = idle((marvel0) => {
        marvel0.schema = "levi.marvel0.status.v2";
        marvel0.units = { state: "unknown", items: [], detail: "x" };
        marvel0.robot_idle = {
          state,
          reasons: [{ source: "guard", state: "busy", text: "t" }],
        };
      });
      expect(banner.state).toBe(state);
      expect(banner.note).toBeNull();
      expect(banner.reasons).toHaveLength(1);
    }
  });

  test("the reasons the server gave stay listed when idle is withdrawn", () => {
    const banner = idle((marvel0) => {
      marvel0.units = { state: "unknown", items: [], detail: "x" };
      marvel0.robot_idle = {
        state: "idle",
        reasons: [{ source: "units", state: "unknown", text: "x" }],
      };
    });
    expect(banner.state).toBe("unknown");
    expect(banner.reasons.map((r) => r.text)).toEqual(["x"]);
  });

  test("an idle verdict that lists reasons contradicts itself: unknown, the reasons stay listed", () => {
    const banner = idle((marvel0) => {
      marvel0.robot_idle = {
        state: "idle",
        reasons: [
          {
            source: "marker",
            state: "busy",
            text: "the arm-controller marker is present (for 2 min)",
          },
        ],
      };
    });
    expect(banner.state).toBe("unknown");
    // The parts were read: "a part could not be read" would be the wrong
    // explanation, the contradiction is the reason.
    expect(flat(banner.note!)).toBe("marvel0.banner.idle_with_reasons");
    expect(banner.reasons.map((r) => r.text)).toEqual([
      "the arm-controller marker is present (for 2 min)",
    ]);
  });

  test("idle with reasons over a part nobody could read: the unread part is named", () => {
    const banner = idle((marvel0) => {
      marvel0.units = { state: "unknown", items: [], detail: "x" };
      marvel0.robot_idle = {
        state: "idle",
        reasons: [{ source: "units", state: "unknown", text: "x" }],
      };
    });
    expect(banner.state).toBe("unknown");
    expect(flat(banner.note!)).toBe("marvel0.banner.idle_unverified");
  });

  test("the two notes are different sentences in both languages", () => {
    expect(en["marvel0.banner.idle_with_reasons"]).toBe(
      "The answer says idle but lists reasons, so it is not trusted.",
    );
    expect(zh["marvel0.banner.idle_with_reasons"]).toBeTruthy();
    expect(zh["marvel0.banner.idle_with_reasons"]).not.toMatch(/[A-Za-z]/);
    for (const catalogue of [en, zh] as Array<Record<string, string>>)
      expect(catalogue["marvel0.banner.idle_with_reasons"]).not.toBe(
        catalogue["marvel0.banner.idle_unverified"],
      );
  });

  test("any entry in the list counts, even one the page cannot read", () => {
    const banner = idle((marvel0) => {
      marvel0.robot_idle = { state: "idle", reasons: [null] };
    });
    expect(banner.state).toBe("unknown");
    expect(flat(banner.note!)).toBe("marvel0.banner.idle_with_reasons");
    expect(banner.reasons).toEqual([]);
  });

  test("an empty list of reasons, or none at all, leaves idle as it is", () => {
    for (const idleAnswer of [
      { state: "idle", reasons: [] },
      { state: "idle" },
    ]) {
      const banner = idle((marvel0) => {
        marvel0.robot_idle = idleAnswer;
      });
      expect(banner.state).toBe("idle");
      expect(banner.note).toBeNull();
    }
  });
});

describe("page-level notes", () => {
  test("no machine status: said, with the failure text", () => {
    const result = marvel0View({
      setup: null,
      marvel0: marvel0Status(),
      setupError: "HTTP 500",
    });
    expect(result.notes.map(flat)).toEqual([
      "marvel0.note.no_setup | HTTP 500",
    ]);
  });

  test("the server's previous sample is flagged with its age", () => {
    const result = view(setupStatus({ stale: true, age_s: 7.5 }));
    expect(result.notes.map(flat)).toEqual([
      "marvel0.note.stale_sample | ago:7.5",
    ]);
  });

  test("a sample older than the window without the server's flag is flagged too", () => {
    const result = view(setupStatus({ age_s: 12 }));
    expect(result.notes.map(flat)).toEqual([
      "marvel0.note.old_sample | ago:12",
    ]);
    expect(view(setupStatus({ age_s: 10 })).notes).toEqual([]);
  });

  test("a status that is still being sampled", () => {
    const result = view({ state: "sampling", cached: false });
    expect(result.notes.map(flat)).toEqual(["marvel0.note.sampling"]);
    expect(cards(result).robot_server.state).toBe("unknown");
  });
});

describe("malformed and missing input", () => {
  const junk: unknown[] = [
    null,
    undefined,
    "text",
    42,
    true,
    [],
    [1, 2],
    {},
    { ports: 3, gpu: "x", live: [], fr3_health: null },
    {
      ports: { state: "ok" },
      gpu: { state: "ok" },
      gpu_locks: { state: "ok" },
    },
    { fr3_health: { state: "ok" } },
    { fr3_health: { state: "ok", hardware_active: {}, controller_active: [] } },
  ];

  test("never throws, and every card has a known state, whatever the answers are", () => {
    for (const setup of junk)
      for (const marvel0 of junk) {
        const result = marvel0View({ setup, marvel0 });
        expect(result.groups.flatMap((g) => g.cards)).toHaveLength(10);
        for (const c of result.groups.flatMap((g) => g.cards))
          expect(STATES).toContain(c.state);
        expect(["idle", "busy", "unknown"]).toContain(result.banner.state);
      }
  });

  test("with nothing to go on, nothing is ok and nothing is not-started", () => {
    for (const setup of junk.filter(
      (x) => !x || typeof x !== "object" || Array.isArray(x),
    ))
      for (const marvel0 of junk.filter(
        (x) => !x || typeof x !== "object" || Array.isArray(x),
      )) {
        const result = marvel0View({ setup, marvel0 });
        for (const c of result.groups.flatMap((g) => g.cards))
          expect(c.state).toBe("unknown");
        expect(result.banner.state).toBe("unknown");
      }
  });

  test("removing the part a card reads makes it unknown, never ok", () => {
    const needs: Array<[Marvel0CardId, string]> = [
      ["desk", "fr3_health"],
      ["arm", "fr3_health"],
      ["robot_server", "ports"],
      ["monitor", "fr3_health"],
      ["policy", "ports"],
      ["vlm", "ports"],
      ["gpu", "gpu"],
    ];
    for (const [id, part] of needs) {
      const setup = setupStatus();
      delete setup[part];
      expect(cards(view(setup))[id].state).toBe("unknown");
    }
    const marvel0 = marvel0Status();
    delete marvel0.eval_sessions;
    expect(cards(view(setupStatus(), marvel0)).demo.state).toBe("unknown");
  });

  test("text from a server is cut to a sane length", () => {
    const long = "x".repeat(5000);
    const c = card(
      "robot_server",
      setupStatus({ ports: { state: "unknown", detail: long } }),
    );
    expect(c.notes).toHaveLength(1);
    expect(serverNotes(c)[0].length).toBeLessThanOrEqual(300);
    const noted = card(
      "desk",
      setupStatus({
        fr3_health: healthPart({ state: "red", reasons: [long] }),
      }),
    );
    expect(serverNotes(noted)[0].length).toBeLessThanOrEqual(300);
  });
});

describe("the server's English sentences stay out of the Chinese page", () => {
  // Every part cannot be read, and says why in a sentence of its own.
  const SETUP_PARTS = ["ports", "gpu", "gpu_locks", "live", "fr3_health"];
  const MARVEL0_PARTS = ["marker", "eval_sessions", "units"];
  const sentence = (part: string) =>
    `English sentence from ${part}: it cannot be read`;
  const unreadable = (part: string) => ({
    state: "unknown",
    detail: sentence(part),
  });
  const found = cards(
    view(
      setupStatus(
        Object.fromEntries(SETUP_PARTS.map((p) => [p, unreadable(p)])),
      ),
      marvel0Status(
        Object.fromEntries(MARVEL0_PARTS.map((p) => [p, unreadable(p)])),
      ),
    ),
  );
  const inZh = (key: string) => (zh as Record<string, string>)[key] ?? key;
  /** Every Say of a card that the page prints outside its notes. */
  const printed = (c: Marvel0Card) =>
    [c.summary, ...c.facts.flatMap((fact) => [fact.label, fact.value])]
      .map((say) => sayText(say, inZh))
      .join("\n");
  // Which parts speak in which card's notes.
  const NOTED: Record<Marvel0CardId, string[]> = {
    desk: ["fr3_health"],
    arm: ["fr3_health", "marker"],
    gripper: [],
    robot_server: ["ports"],
    monitor: ["fr3_health"],
    policy: ["ports", "units"],
    demo: ["eval_sessions"],
    live: ["live", "ports"],
    vlm: ["ports"],
    gpu: ["gpu", "gpu_locks"],
  };

  test("every card is unknown, so each one fell back to its own wording", () => {
    for (const c of Object.values(found)) expect(c.state).toBe("unknown");
  });

  test("no summary or fact holds a server sentence, in Chinese or in English", () => {
    for (const c of Object.values(found))
      for (const part of [...SETUP_PARTS, ...MARVEL0_PARTS]) {
        expect(printed(c)).not.toContain(sentence(part));
        expect(flat(c.summary)).not.toContain(sentence(part));
        for (const fact of c.facts)
          expect(flat(fact.value)).not.toContain(sentence(part));
      }
  });

  test("the sentences are in the notes of the cards that read their parts, once each", () => {
    for (const c of Object.values(found))
      expect([...c.notes].sort()).toEqual(NOTED[c.id].map(sentence).sort());
  });
});

describe("a sample of the machine status that is too old", () => {
  const FROM_SETUP: Marvel0CardId[] = [
    "desk",
    "arm",
    "robot_server",
    "monitor",
    "policy",
    "live",
    "vlm",
    "gpu",
  ];
  const old = (over: Record<string, unknown> = {}) =>
    setupStatus({ stale: true, age_s: 42, ...over });

  test("every card read from it is stale and says how old the sample is", () => {
    const found = cards(view(old()));
    for (const id of FROM_SETUP) {
      expect(found[id].state).toBe("stale");
      expect(found[id].summary).toEqual({
        key: "marvel0.card.old_sample",
        params: { age: { ago: 42 } },
      });
    }
  });

  test("what the cards do not read from it is left alone", () => {
    const found = cards(view(old()));
    // The gripper stack has no source; the demo client comes from the marvel0 status.
    expect(found.gripper.state).toBe("unknown");
    expect(summary(found.gripper)).toBe("marvel0.gripper.reason");
    expect(found.demo.state).toBe("ok");
  });

  test("the readings stay as facts, the old verdict as the first of them", () => {
    const found = cards(view(old()));
    expect(factText(found.robot_server, "marvel0.fact.in_sample")).toEqual([
      'marvel0.server.ok {"port":5000}',
    ]);
    expect(found.robot_server.facts.map((fact) => flat(fact.label))[0]).toBe(
      "marvel0.fact.in_sample",
    );
    expect(factText(found.robot_server, "marvel0.fact.port")).toEqual([
      "marvel0.port.listening | python",
    ]);
    expect(factText(found.gpu, "GPU memory")[0]).toContain("12.0 / 32.0 GiB");
    expect(factText(found.live, "marvel0.fact.service")).toEqual(["idle"]);
    expect(factText(found.desk, "Robot mode")).toEqual(["Idle (1)"]);
  });

  test("a red light in an old sample is not shown as red, but is not lost", () => {
    const found = cards(
      view(
        old({
          fr3_health: healthPart({
            state: "red",
            robot_mode: 4,
            robot_mode_name: "Reflex",
            reasons: ["cartesian reflex"],
          }),
        }),
      ),
    );
    for (const id of ["desk", "arm", "monitor"] as const) {
      expect(found[id].state).toBe("stale");
      expect(found[id].notes).toContain("cartesian reflex");
    }
    expect(factText(found.desk, "marvel0.fact.in_sample")).toEqual([
      "The FR3 health monitor reports the red light.",
    ]);
  });

  test("a card with nothing to show stays unknown", () => {
    const found = cards(view(old({ fr3_health: { state: "unknown" } })));
    expect(found.desk.state).toBe("unknown");
    expect(found.arm.state).toBe("unknown");
    expect(summary(found.desk)).toBe("marvel0.health.unreadable");
    expect(found.robot_server.state).toBe("stale");
    const quiet = cards(
      view({ ...quietSetup(), stale: true, age_s: 42 }, quietMarvel0()),
    );
    expect(quiet.desk.state).toBe("unknown");
    expect(quiet.robot_server.state).toBe("stale");
  });

  test("an age beyond the freshness window does the same without the server's flag", () => {
    const found = cards(view(setupStatus({ age_s: 10.5 })));
    for (const id of FROM_SETUP) {
      expect(found[id].state).toBe("stale");
      expect(found[id].summary).toEqual({
        key: "marvel0.card.old_sample",
        params: { age: { ago: 10.5 } },
      });
    }
  });

  test("an age at the window, or no age, and a flag that is not true leave the cards alone", () => {
    for (const over of [
      { age_s: MARVEL0_FRESH_MS / 1000 },
      { age_s: 0 },
      { age_s: undefined },
      { age_s: "99" },
      { stale: false, age_s: 3 },
      { stale: "true", age_s: 3 },
      { stale: 1, age_s: 3 },
    ])
      expect(cards(view(setupStatus(over))).robot_server.state).toBe("ok");
  });

  test("a flag with no age still marks the cards, with a dash for the age", () => {
    const c = card("robot_server", setupStatus({ stale: true, age_s: null }));
    expect(c.state).toBe("stale");
    expect(c.summary).toEqual({
      key: "marvel0.card.old_sample",
      params: { age: { ago: null } },
    });
  });
});

describe("what the idle reasons say, from their code and params", () => {
  type Coded = [
    source: string,
    state: string,
    code: string,
    params: Record<string, string | number>,
  ];
  // The contract: every code the marvel0 status emits, with params as it emits them.
  const CODED: Coded[] = [
    ["marker", "busy", "marker.present", { age_s: 95 }],
    ["marker", "unknown", "marker.old", { age_s: 90000 }],
    ["marker", "unknown", "marker.unreadable", {}],
    ["marker", "unknown", "marker.no_folder", {}],
    [
      "eval_sessions",
      "busy",
      "sessions.recent",
      { group: "pi05_run_a", age_s: 12, status: "running" },
    ],
    [
      "eval_sessions",
      "unknown",
      "sessions.silent",
      { group: "pi05_run_a", age_s: 200, status: "running" },
    ],
    ["eval_sessions", "unknown", "sessions.unreadable", {}],
    [
      "units",
      "busy",
      "units.active",
      {
        name: "levi-aeri-policy-pi05.service",
        active: "active",
        sub: "running",
      },
    ],
    [
      "units",
      "unknown",
      "units.state_unknown",
      { name: "levi-aeri-run.service", active: "maintenance" },
    ],
    ["units", "unknown", "units.unreadable", {}],
    [
      "guard",
      "busy",
      "guard.robot_process",
      { names: "franka_hw, ros2_control_node" },
    ],
    ["guard", "busy", "guard.controller_active", {}],
    ["guard", "unknown", "guard.unreadable", {}],
    ["guard", "unknown", "guard.not_quiet", {}],
    ["guard", "unknown", "guard.old_sample", {}],
    ["guard", "unknown", "guard.truncated", {}],
    ["guard", "unknown", "guard.controller_value", {}],
  ];
  const wire = ([source, state, code, params]: Coded) => ({
    source,
    state,
    text: `SERVER SENTENCE ${code}`,
    code,
    params,
  });
  const reasonsOf = (...items: unknown[]) =>
    view(
      setupStatus(),
      marvel0Status({ robot_idle: { state: "busy", reasons: items } }),
    ).banner.reasons;
  const inEn = (key: string) => (en as Record<string, string>)[key] ?? key;
  const inZh = (key: string) => (zh as Record<string, string>)[key] ?? key;
  /** The text without the values the server put in the params. */
  const bare = (text: string, params: Record<string, string | number>) =>
    Object.values(params)
      .map(String)
      .sort((a, b) => b.length - a.length)
      .reduce((out, value) => out.split(value).join(""), text);

  test("the contract has seventeen distinct codes, each under the source it belongs to", () => {
    expect(new Set(CODED.map(([, , code]) => code)).size).toBe(17);
    for (const [source, , code] of CODED)
      expect(code.split(".")[0]).toBe(
        source === "eval_sessions" ? "sessions" : source,
      );
  });

  test.each(CODED)(
    "%s %s %s is worded in both languages, with no sentence of the server's",
    (...coded) => {
      const [reason] = reasonsOf(wire(coded));
      const params = coded[3];
      expect(reason.say).not.toBeNull();
      expect(reason.text).toBe(`SERVER SENTENCE ${coded[2]}`);
      const english = sayText(reason.say!, inEn);
      const chinese = sayText(reason.say!, inZh);
      for (const text of [english, chinese]) {
        expect(text).not.toContain("SERVER");
        // Every placeholder was filled.
        expect(text).not.toMatch(/[{}]/);
        expect(text.length).toBeGreaterThan(5);
        // The values the server gave are shown as they are.
        for (const value of Object.values(params).filter(
          (v) => typeof v === "string",
        ))
          expect(text).toContain(String(value));
      }
      // The Chinese wording has no English left once the server's own values
      // (names, states) are taken out.
      expect(bare(chinese, params)).not.toMatch(/[A-Za-z]/);
      expect(chinese).not.toBe(english);
    },
  );

  test("an age in the params is worded as an age", () => {
    const [present] = reasonsOf(wire(CODED[0]));
    // 95 s is worded in minutes, like every age on the page.
    expect(sayText(present.say!, inEn)).toContain("2 min ago");
    expect(sayText(present.say!, inZh)).toContain("2分钟前");
    const [old] = reasonsOf(wire(CODED[1]));
    expect(sayText(old.say!, inEn)).toContain("25 h ago");
  });

  test("a value the server left out is said to be unknown, not made up", () => {
    const [noAge] = reasonsOf({ ...wire(CODED[0]), params: {} });
    expect(sayText(noAge.say!, inZh)).toContain(zh["marvel0.value.unknown"]);
    const [noStatus] = reasonsOf({
      ...wire(CODED[4]),
      params: { group: "g", age_s: 5 },
    });
    expect(sayText(noStatus.say!, inEn)).toContain(en["marvel0.value.unknown"]);
    expect(sayText(noStatus.say!, inEn)).toContain("5 s ago");
    const [noSub] = reasonsOf({
      ...wire(CODED[7]),
      params: { name: "u.service", active: "active" },
    });
    expect(sayText(noSub.say!, inEn)).toContain(en["marvel0.value.unknown"]);
  });

  test("a code the page does not know falls back to the server's sentence", () => {
    const reasons = reasonsOf(
      { source: "units", state: "busy", text: "t1", code: "units.new_kind" },
      { source: "units", state: "busy", text: "t2", code: "constructor" },
      { source: "units", state: "busy", text: "t3", code: "__proto__" },
      { source: "units", state: "busy", text: "t4", code: "" },
      { source: "units", state: "busy", text: "t5", code: 7 },
      { source: "units", state: "busy", text: "t6" },
    );
    expect(reasons.map((r) => [r.say, r.text])).toEqual([
      [null, "t1"],
      [null, "t2"],
      [null, "t3"],
      [null, "t4"],
      [null, "t5"],
      [null, "t6"],
    ]);
  });

  test("a known code whose params do not fit it falls back too", () => {
    const [units, sessions, bad, nested] = reasonsOf(
      { ...wire(CODED[7]), params: { active: "active" } },
      { ...wire(CODED[4]), params: { group: 5, age_s: 3 } },
      { ...wire(CODED[4]), params: "group=a" },
      { ...wire(CODED[7]), params: { name: { x: 1 }, active: "active" } },
    );
    for (const reason of [units, sessions, bad, nested])
      expect(reason.say).toBeNull();
    // Params that are not there are no params; only a code that needs some fails.
    const [plain] = reasonsOf({ ...wire(CODED[3]), params: undefined });
    expect(plain.say).not.toBeNull();
  });

  test("an answer without codes (an older server) shows the server's sentences as before", () => {
    const [reason] = reasonsOf({
      source: "marker",
      state: "busy",
      text: "the arm-controller marker is present",
    });
    expect(reason.say).toBeNull();
    expect(reason.text).toBe("the arm-controller marker is present");
  });

  test("busy reasons come before the ones that cannot tell, the rest last, each in the server's order", () => {
    const item = (state: string | undefined, text: string) => ({
      source: "units",
      state,
      text,
    });
    const reasons = reasonsOf(
      item("unknown", "u1"),
      item("present", "x1"),
      item("busy", "b1"),
      item(undefined, "u2"),
      item("busy", "b2"),
      item("unknown", "u3"),
    );
    expect(reasons.map((r) => r.text)).toEqual([
      "b1",
      "b2",
      "u1",
      "u2",
      "u3",
      "x1",
    ]);
  });

  test("the cap of twelve is applied after the ordering", () => {
    const unknown = Array.from({ length: 12 }, (_, i) => ({
      source: "units",
      state: "unknown",
      text: `u${i}`,
    }));
    const reasons = reasonsOf(...unknown, {
      source: "guard",
      state: "busy",
      text: "late busy one",
    });
    expect(reasons).toHaveLength(12);
    expect(reasons[0].text).toBe("late busy one");
    expect(reasons[11].text).toBe("u10");
  });

  describe("card notes", () => {
    const wireReason = (index: number) => wire(CODED[index]);
    const unread = (extra: Record<string, unknown> = {}) =>
      marvel0Status({
        marker: { state: "unknown", detail: "SERVER marker sentence" },
        eval_sessions: {
          state: "unknown",
          roots: 1,
          sessions: [],
          detail: "SERVER sessions sentence",
        },
        units: {
          state: "unknown",
          items: [],
          detail: "SERVER units sentence",
        },
        robot_idle: {
          state: "unknown",
          reasons: [wireReason(2), wireReason(6), wireReason(9)],
        },
        ...extra,
      });
    const found = (marvel0: unknown) => cards(view(setupStatus(), marvel0));

    test("a part that cannot be read is worded from the code the verdict gives it", () => {
      const result = found(unread());
      expect(result.arm.notes).toEqual([
        { key: "marvel0.reason.marker_unreadable" },
      ]);
      expect(result.demo.notes).toEqual([
        { key: "marvel0.reason.sessions_unreadable" },
      ]);
      expect(result.policy.notes).toEqual([
        { key: "marvel0.reason.units_unreadable" },
      ]);
      // The server's sentence is gone from the page, not just moved.
      for (const c of Object.values(result))
        expect(serverNotes(c).join("\n")).not.toContain("SERVER");
    });

    test("a marker folder that does not exist has its own words", () => {
      const result = found(
        unread({
          marker: { state: "unknown", detail: "SERVER marker sentence" },
          robot_idle: { state: "unknown", reasons: [wireReason(3)] },
        }),
      );
      expect(result.arm.notes).toEqual([
        { key: "marvel0.reason.marker_no_folder" },
      ]);
    });

    test("without a code the page knows for that part, the server's sentence stays a note", () => {
      for (const reasons of [
        [],
        [{ ...wireReason(2), code: "marker.something_new" }],
        // A code that belongs to another part does not speak for this one.
        [{ ...wireReason(6), source: "marker" }],
        // Nor does a code of the same part that says something else.
        [{ ...wireReason(1), code: "marker.old" }],
      ]) {
        const result = found(
          unread({ robot_idle: { state: "unknown", reasons } }),
        );
        expect(result.arm.notes).toEqual(["SERVER marker sentence"]);
        expect(result.demo.notes).toEqual(["SERVER sessions sentence"]);
        expect(result.policy.notes).toEqual(["SERVER units sentence"]);
        expect(result.arm.notes.some(isSay)).toBe(false);
      }
    });

    test("parts that are fine have no note of this kind", () => {
      const result = found(marvel0Status());
      expect(result.arm.notes).toEqual([]);
      expect(result.policy.notes).toEqual([]);
      expect(result.demo.notes).toEqual([]);
    });
  });
});

describe("polling rules", () => {
  test("3 s, doubling after failed rounds up to 30 s", () => {
    expect(marvel0Delay(0)).toBe(MARVEL0_POLL_MS);
    expect(MARVEL0_POLL_MS).toBe(3000);
    expect([1, 2, 3, 4, 5].map(marvel0Delay)).toEqual([
      6000, 12000, 24000, 30000, 30000,
    ]);
    expect(marvel0Delay(99)).toBe(MARVEL0_POLL_MAX_MS);
    expect(marvel0Delay(-1)).toBe(MARVEL0_POLL_MS);
  });

  test("an answer may be shown as current for a few rounds only", () => {
    expect(isFresh(null, 1000)).toBe(false);
    expect(isFresh(1000, 1000 + MARVEL0_FRESH_MS)).toBe(true);
    expect(isFresh(1000, 1001 + MARVEL0_FRESH_MS)).toBe(false);
    expect(MARVEL0_FRESH_MS).toBeGreaterThan(3 * MARVEL0_POLL_MS);
  });

  test("the age the sample already had when it arrived counts too", () => {
    // Arrived 4 s ago with a sample 6 s old: 10 s in all.
    expect(isFresh(1000, 1000 + 4000, 6)).toBe(true);
    expect(isFresh(1000, 1001 + 4000, 6)).toBe(false);
    // Old on arrival.
    expect(isFresh(1000, 1000, 10.5)).toBe(false);
    expect(isFresh(1000, 1000, 10)).toBe(true);
    // No age, or one that is no number, adds nothing.
    for (const age of [undefined, null, Number.NaN, -3])
      expect(isFresh(1000, 1000 + MARVEL0_FRESH_MS, age)).toBe(true);
    expect(isFresh(null, 1000, 0)).toBe(false);
  });

  test("an answer's age is the setup status' age_s, zero when it does not say", () => {
    expect(sampleAgeS({ age_s: 3.5 })).toBe(3.5);
    for (const answer of [
      null,
      "x",
      [],
      {},
      { age_s: "3" },
      { age_s: Number.NaN },
      { age_s: -1 },
      marvel0Status(),
    ])
      expect(sampleAgeS(answer)).toBe(0);
  });
});

describe("words", () => {
  test("every state has badge words, a tone and a Chinese name", () => {
    for (const state of STATES) {
      expect(STATE_KEY[state] in en).toBe(true);
      expect(STATE_KEY[state] in zh).toBe(true);
      expect(STATE_TONE[state]).toBeTruthy();
    }
    expect(STATE_TONE.stale).not.toBe(STATE_TONE.red);
    expect(STATE_TONE.not_started).toBe("neutral");
  });

  test("the badge of a red light says Fault, and the cards that mirror the monitor still say red light", () => {
    expect(en["marvel0.state.red"]).toBe("Fault");
    expect(zh["marvel0.state.red"]).toBe("故障");
    const red = cards(
      view(setupStatus({ fr3_health: healthPart({ state: "red" }) })),
    );
    for (const id of ["desk", "arm", "monitor"] as const) {
      expect(red[id].state).toBe("red");
      const shown = sayText(
        red[id].summary,
        (key) => (en as Record<string, string>)[key] ?? key,
      );
      expect(shown).toContain("red light");
      expect(
        sayText(
          red[id].summary,
          (key) => (zh as Record<string, string>)[key] ?? key,
        ),
      ).toContain("红灯");
    }
    // The stack-down wording names the light too.
    expect(en["marvel0.arm.red_follows"]).toContain("red light");
    expect(zh["marvel0.arm.red_follows"]).toContain("红灯");
  });

  test("the monitor's card is named after its terminal, like the others", () => {
    expect(en["marvel0.monitor.title"]).toBe("Terminal M: health monitor");
    expect(zh["marvel0.monitor.title"]).toBe("终端 M：健康监视器");
  });

  test("the idle banner claims no more than that nothing was seen", () => {
    expect(en["marvel0.idle.idle"]).toBe("No robot activity seen");
    expect(zh["marvel0.idle.idle"]).toBe("未发现机器人活动");
    expect(en["marvel0.idle.busy"]).toBe("Looks busy");
  });

  /** Every catalogue key and `{placeholder}` a view comes to. */
  function used(say: Say, into: Array<{ key: string; params: string[] }>) {
    if ("join" in say) say.join.forEach((part) => used(part, into));
    else if ("key" in say) {
      into.push({ key: say.key, params: Object.keys(say.params ?? {}) });
      for (const value of Object.values(say.params ?? {}))
        if (typeof value === "object") used(value, into);
    }
  }

  test("the keys of every view exist in both catalogues and their placeholders are filled", () => {
    const views = [
      view(setupStatus()),
      view(quietSetup(), quietMarvel0()),
      view(null, null),
      marvel0View({
        setup: null,
        marvel0: null,
        setupError: "x",
        marvel0Error: "y",
      }),
      view(
        setupStatus({ stale: true }),
        marvel0Status({ marker: { state: "present", age_s: 99999 } }),
      ),
      view(
        setupStatus({
          fr3_health: healthPart({
            state: "red",
            hardware_active: false,
            controller_active: false,
            reasons: ["robot_state_no_data 4s"],
          }),
        }),
        marvel0Status({
          eval_sessions: {
            state: "unknown",
            sessions: [{ group: "g", age_s: 1, status: "finished" }],
            detail: "d",
          },
          robot_idle: {
            state: "busy",
            reasons: [
              { source: "guard", state: "unknown", text: "a" },
              { source: "units", state: "busy", text: "b" },
              { source: "marker", state: "other", text: "c" },
            ],
          },
        }),
      ),
      view(setupStatus(), {
        ...quietMarvel0(),
        schema: "levi.marvel0.status.v2",
      }),
      view(setupStatus(), {
        ...quietMarvel0(),
        units: { state: "unknown", items: [], detail: "x" },
      }),
      view(
        setupStatus({
          fr3_health: healthPart({ state: "red", hardware_active: false }),
          live: { state: "unknown" },
          ports: portsPart([]),
          gpu_locks: {
            state: "ok",
            locks: [
              { label: "x", state: "absent" },
              { label: "y", state: "unknown" },
            ],
          },
          gpu: gpuPart({ reused: true }),
        }),
        marvel0Status({
          units: {
            state: "ok",
            items: [
              { name: "levi-aeri-policy-a", active: "failed", sub: "failed" },
            ],
          },
          eval_sessions: {
            state: "ok",
            sessions: [{ group: "g", age_s: 1, status: "fault" }],
          },
        }),
      ),
    ];
    const keys: Array<{ key: string; params: string[] }> = [];
    for (const result of views) {
      used(result.banner.note ?? { join: [] }, keys);
      result.banner.reasons.forEach((r) => {
        used(r.source, keys);
        used(r.state, keys);
      });
      result.notes.forEach((n) => used(n, keys));
      for (const group of result.groups) {
        keys.push({ key: group.title, params: [] });
        for (const c of group.cards) {
          keys.push({ key: c.title, params: [] }, { key: c.hint, params: [] });
          used(c.summary, keys);
          for (const fact of c.facts) {
            used(fact.label, keys);
            used(fact.value, keys);
          }
        }
      }
    }
    expect(keys.length).toBeGreaterThan(60);
    const catalogue = {
      en: en as Record<string, string>,
      zh: zh as Record<string, string>,
    };
    const problems: string[] = [];
    for (const { key, params } of keys)
      for (const [name, text] of Object.entries(catalogue)) {
        if (!text[key]) {
          problems.push(`${name}: missing ${key}`);
          continue;
        }
        for (const match of text[key].matchAll(/\{(\w+)\}/g))
          if (!params.includes(match[1]))
            problems.push(`${name}: ${key} needs {${match[1]}}`);
      }
    expect([...new Set(problems)]).toEqual([]);
  });
});

describe("the stack status strip", () => {
  test("lists M, Desk, A, B, C, the policy server and the client, each with its card's state", () => {
    const result = view(setupStatus());
    expect(result.stack.steps.map((step) => step.id)).toEqual([
      "monitor",
      "desk",
      "arm",
      "gripper",
      "robot_server",
      "policy",
      "demo",
    ]);
    const found = cards(result);
    for (const step of result.stack.steps)
      expect(step.state).toBe(found[step.id].state);
    expect(result.stack.cause).toBeNull();
  });

  test("every short name is in both catalogues", () => {
    for (const { label } of STACK_ORDER) {
      expect(en).toHaveProperty([label]);
      expect(zh).toHaveProperty([label]);
    }
  });

  test("a health file that stopped updating is named once as the reason Desk, A and M are stale", () => {
    const result = view(
      setupStatus({ fr3_health: healthPart({ state: "offline", age_s: 420 }) }),
    );
    const found = cards(result);
    expect([found.desk.state, found.arm.state, found.monitor.state]).toEqual([
      "stale",
      "stale",
      "stale",
    ]);
    expect(result.stack.cause).toEqual({
      key: "marvel0.cause.monitor_offline",
      params: { age: { ago: 420 } },
    });
  });

  test("no health file at all is named as the reason Desk and A are unknown", () => {
    const result = view(setupStatus({ fr3_health: { state: "missing" } }));
    expect(result.stack.cause).toEqual({
      key: "marvel0.cause.monitor_missing",
    });
  });

  test("an old sample of the whole machine status gives no monitor reason: the page note says why", () => {
    const result = view(
      setupStatus({
        stale: true,
        age_s: 30,
        fr3_health: healthPart({ state: "offline", age_s: 420 }),
      }),
    );
    expect(result.notes.length).toBeGreaterThan(0);
    expect(result.stack.cause).toBeNull();
  });

  test("terminal B stays unknown with its reason first: the cards read no process list", () => {
    const busy = setupStatus({
      guard: {
        state: "ok",
        quiet: false,
        robot: [
          { pid: 1, name: "ros2_control_node" },
          { pid: 2, name: "ros2_control_node" },
          { pid: 3, name: "franka_server_ros2" },
        ],
        banner: null,
      },
    });
    const c = card("gripper", busy);
    expect(c.state).toBe("unknown");
    expect(summary(c)).toBe("marvel0.gripper.reason");
    expect(en["marvel0.gripper.reason"]).toMatch(/^Unknown: /);
    expect(zh["marvel0.gripper.reason"]).toMatch(/^未知：/);
    // RIG-01: marvel0 uses passive sources only, so no card of the arm stack
    // carries anything from the guard's process list.
    for (const id of ["arm", "gripper", "robot_server"] as const) {
      const text = JSON.stringify(card(id, busy));
      expect(text).not.toContain("ros2_control_node");
      expect(text).not.toContain("franka_server");
    }
  });
});
