import { describe, expect, test } from "bun:test";
import en from "@/i18n/en.json";
import { actView } from "../stack-act-logic";
import { attestBody, dialogReady, dialogSpec } from "../stack-dialog-logic";
import { stackView } from "../stack-status-logic";
import { stackDoc } from "./ctl-fixtures";

const KNOWN = new Set(Object.keys(en));
const act = (name: string) =>
  actView(stackDoc(name), KNOWN, stackView(stackDoc(name), KNOWN));

describe("what each confirmation asks", () => {
  test("starting A: the e-stop sentence, the hazard, the two checks of the person, the phrase to type", () => {
    const spec = dialogSpec(
      { kind: "start", comp: "A" },
      act("ready_to_start_a"),
    );
    expect(spec.estop).toBe(true);
    expect(spec.hazards).toEqual([{ key: "marvel0.ctl.hazard.start_A" }]);
    expect(spec.attest.map((a) => a.code)).toEqual(["desk_done", "estop_hand"]);
    expect(spec.phrase).toBe("start arm stack");
    expect(spec.typed).toBe(true);
    expect(spec.danger).toBe(false);
  });

  test("starting B names the jaws; starting C names the sag and the open port and asks three checks", () => {
    const b = dialogSpec({ kind: "start", comp: "B" }, act("a_ready"));
    expect(b.hazards).toEqual([{ key: "marvel0.ctl.hazard.start_B" }]);
    expect(b.attest.map((a) => a.code)).toEqual(["hands_clear"]);
    const c = dialogSpec({ kind: "start", comp: "C" }, act("b_ready"));
    expect(c.hazards.map((h) => ("key" in h ? h.key : ""))).toEqual([
      "marvel0.ctl.hazard.start_C_sag",
      "marvel0.ctl.hazard.start_C_port",
    ]);
    expect(c.attest.map((a) => a.code)).toEqual([
      "estop_hand",
      "hands_clear",
      "no_client",
    ]);
    expect(c.phrase).toBe("start robot server");
  });

  test("starting the monitor needs no e-stop sentence and no ticks", () => {
    const spec = dialogSpec({ kind: "start", comp: "M" }, act("idle"));
    expect(spec.estop).toBe(false);
    expect(spec.attest).toEqual([]);
    expect(spec.phrase).toBe("start health monitor");
  });

  test("stopping C says the arm stays under impedance control; with a client it asks for the override phrase at once", () => {
    const plain = dialogSpec({ kind: "stop", comp: "C" }, act("c_ready"));
    expect(plain.hazards).toEqual([{ key: "marvel0.ctl.hazard.stop_C" }]);
    expect(plain.danger).toBe(true);
    expect(plain.override).toEqual({
      phrase: "stop robot server with client connected",
      now: false,
    });
    const withClient = dialogSpec(
      { kind: "stop", comp: "C" },
      act("c_ready_with_client"),
    );
    expect(withClient.override).toEqual({
      phrase: "stop robot server with client connected",
      now: true,
    });
    expect(
      dialogSpec({ kind: "stop", comp: "A" }, act("a_ready")).override,
    ).toBeNull();
  });

  test("the gripper test is a tick and a click: the phrase is sent by the page, not typed", () => {
    const spec = dialogSpec(
      { kind: "gripper", action: "open" },
      act("b_ready"),
    );
    expect(spec.typed).toBe(false);
    expect(spec.phrase).toBe("gripper open");
    expect(spec.attest.map((a) => a.code)).toEqual(["hands_clear"]);
    expect(spec.hazards).toEqual([{ key: "marvel0.ctl.hazard.gripper" }]);
    expect(
      dialogSpec({ kind: "gripper", action: "close" }, act("b_ready")).phrase,
    ).toBe("gripper close");
  });

  test("the marker: a click to set or adopt it, a typed phrase to release it", () => {
    expect(
      dialogSpec({ kind: "marker", op: "touch" }, act("idle")),
    ).toMatchObject({ phrase: "set marker", typed: false });
    expect(
      dialogSpec({ kind: "marker", op: "adopt" }, act("idle")),
    ).toMatchObject({ phrase: "adopt marker", typed: false });
    expect(
      dialogSpec({ kind: "marker", op: "release" }, act("idle")),
    ).toMatchObject({ phrase: "clear marker", typed: true, danger: true });
  });

  test("the guided start asks A's two checks and says that it stops before B and C", () => {
    const spec = dialogSpec({ kind: "sequence" }, act("idle"));
    expect(spec.attest.map((a) => a.code)).toEqual(["desk_done", "estop_hand"]);
    expect(spec.phrase).toBe("start arm stack");
  });
});

describe("when the confirm button is on", () => {
  const spec = dialogSpec(
    { kind: "start", comp: "A" },
    act("ready_to_start_a"),
  );
  const ticked = { desk_done: true, estop_hand: true };

  test("only with the phrase typed exactly and every check ticked", () => {
    expect(dialogReady(spec, "start arm stack", ticked, "", false)).toBe(true);
    expect(dialogReady(spec, " start arm stack ", ticked, "", false)).toBe(
      true,
    );
    expect(dialogReady(spec, "start arm stack!", ticked, "", false)).toBe(
      false,
    );
    expect(dialogReady(spec, "Start arm stack", ticked, "", false)).toBe(false);
    expect(
      dialogReady(spec, "start arm stack", { desk_done: true }, "", false),
    ).toBe(false);
    expect(
      dialogReady(
        spec,
        "start arm stack",
        { desk_done: true, estop_hand: false },
        "",
        false,
      ),
    ).toBe(false);
    expect(dialogReady(spec, "", {}, "", false)).toBe(false);
  });

  test("a dialog without a typed phrase needs only its ticks", () => {
    const grip = dialogSpec(
      { kind: "gripper", action: "open" },
      act("b_ready"),
    );
    expect(dialogReady(grip, "", {}, "", false)).toBe(false);
    expect(dialogReady(grip, "", { hands_clear: true }, "", false)).toBe(true);
  });

  test("with a client on C the override phrase is needed as well", () => {
    const stop = dialogSpec(
      { kind: "stop", comp: "C" },
      act("c_ready_with_client"),
    );
    expect(dialogReady(stop, "stop robot server", {}, "", true)).toBe(false);
    expect(
      dialogReady(
        stop,
        "stop robot server",
        {},
        "stop robot server with client connected",
        true,
      ),
    ).toBe(true);
  });

  test("the request carries every check of the spec as true, and nothing else", () => {
    expect(attestBody(spec)).toEqual({ desk_done: true, estop_hand: true });
    expect(
      attestBody(dialogSpec({ kind: "stop", comp: "C" }, act("c_ready"))),
    ).toEqual({});
  });
});
