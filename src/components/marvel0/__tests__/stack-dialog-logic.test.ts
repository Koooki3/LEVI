import { describe, expect, test } from "bun:test";
import en from "@/i18n/en.json";
import { actView } from "../stack-act-logic";
import {
  attestBody,
  dialogReady,
  dialogSpec,
  overrideReady,
} from "../stack-dialog-logic";
import { stackView } from "../stack-status-logic";
import { stackDoc } from "./ctl-fixtures";

const KNOWN = new Set(Object.keys(en));
const act = (name: string) =>
  actView(stackDoc(name), KNOWN, stackView(stackDoc(name), KNOWN));

describe("what each confirmation asks", () => {
  test("starting A: the e-stop sentence, the hazard, the two checks of the person, the phrase the page sends", () => {
    const spec = dialogSpec(
      { kind: "start", comp: "A" },
      act("ready_to_start_a"),
    );
    expect(spec.estop).toBe(true);
    expect(spec.hazards).toEqual([{ key: "marvel0.ctl.hazard.start_A" }]);
    expect(spec.attest.map((a) => a.code)).toEqual(["desk_done", "estop_hand"]);
    expect(spec.phrase).toBe("start arm stack");
    expect(spec.danger).toBe(false);
    // Nothing is typed: the only text a person types is an override.
    expect(spec).not.toHaveProperty("typed");
    expect(spec.override).toBeNull();
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
    expect(spec.phrase).toBe("gripper open");
    expect(spec.attest.map((a) => a.code)).toEqual(["hands_clear"]);
    expect(spec.hazards).toEqual([{ key: "marvel0.ctl.hazard.gripper" }]);
    expect(
      dialogSpec({ kind: "gripper", action: "close" }, act("b_ready")).phrase,
    ).toBe("gripper close");
  });

  test("the marker: a click to set, adopt or release it; releasing is the danger button", () => {
    expect(
      dialogSpec({ kind: "marker", op: "touch" }, act("idle")),
    ).toMatchObject({ phrase: "set marker", danger: false });
    expect(
      dialogSpec({ kind: "marker", op: "adopt" }, act("idle")),
    ).toMatchObject({ phrase: "adopt marker", danger: false });
    expect(
      dialogSpec({ kind: "marker", op: "release" }, act("idle")),
    ).toMatchObject({ phrase: "clear marker", danger: true });
  });

  test("the whole stack's start carries every hazard line and every check of the single starts of A, B and C, once, and offers the pause", () => {
    const idle = act("idle");
    const spec = dialogSpec({ kind: "sequence" }, idle);
    const single = (["A", "B", "C"] as const).map((comp) =>
      dialogSpec({ kind: "start", comp }, idle),
    );
    // The checks: those of A, B and C, each once, in the order they come up.
    expect(spec.attest.map((a) => a.code)).toEqual([
      "desk_done",
      "estop_hand",
      "hands_clear",
      "no_client",
    ]);
    for (const one of single)
      for (const check of one.attest)
        expect(spec.attest.map((a) => a.code)).toContain(check.code);
    // The hazards: the line that says nothing asks again, then each single
    // window's own lines, none missing.
    const keys = spec.hazards.map((h) => ("key" in h ? h.key : ""));
    expect(keys[0]).toBe("marvel0.ctl.hazard.sequence_through");
    expect(keys.slice(1)).toEqual(
      single.flatMap((one) =>
        one.hazards.map((h) => ("key" in h ? h.key : "")),
      ),
    );
    expect(keys).toEqual(
      expect.arrayContaining([
        "marvel0.ctl.hazard.start_A",
        "marvel0.ctl.hazard.start_B",
        "marvel0.ctl.hazard.start_C_sag",
        "marvel0.ctl.hazard.start_C_port",
      ]),
    );
    expect(spec.estop).toBe(true);
    expect(spec.phrase).toBe("start arm stack");
    expect(spec.danger).toBe(false);
    // The pause is a choice of this window alone.
    expect(spec.pauseOption).toBe(true);
    for (const other of [
      ...single,
      dialogSpec({ kind: "sequence_stop" }, idle),
      dialogSpec({ kind: "marker", op: "touch" }, idle),
      dialogSpec({ kind: "gripper", action: "open" }, idle),
    ])
      expect(other.pauseOption).toBe(false);
    // Every check is needed before Confirm is on; the pause is not one.
    expect(dialogReady(spec, {}, "", false)).toBe(false);
    expect(
      dialogReady(
        spec,
        { desk_done: true, estop_hand: true, hands_clear: true },
        "",
        false,
      ),
    ).toBe(false);
    const all = attestBody(spec);
    expect(all).toEqual({
      desk_done: true,
      estop_hand: true,
      hands_clear: true,
      no_client: true,
    });
    expect(dialogReady(spec, all, "", false)).toBe(true);
  });

  test("the stop of the whole stack: C, then A, then B; the arm stays held until A stops; no ticks, no override", () => {
    const spec = dialogSpec({ kind: "sequence_stop" }, act("c_ready"));
    expect(spec.hazards.map((h) => ("key" in h ? h.key : ""))).toEqual([
      "marvel0.ctl.hazard.stop_all_order",
      "marvel0.ctl.hazard.stop_all_arm",
      "marvel0.ctl.hazard.stop_all_client",
    ]);
    expect(spec.estop).toBe(true);
    expect(spec.danger).toBe(true);
    expect(spec.attest).toEqual([]);
    expect(spec.override).toBeNull();
    expect(spec.confirmKey).toBe("marvel0.ctl.btn.stop_all");
    // The page's phrase is the backend's when it names one, else a constant.
    expect(spec.phrase).toBe("stop whole stack");
    const named = actView(
      stackDoc("c_ready", (doc) => {
        doc.act.phrases = { ...doc.act.phrases, sequence_stop: "stop all" };
      }),
      KNOWN,
    );
    expect(dialogSpec({ kind: "sequence_stop" }, named).phrase).toBe(
      "stop all",
    );
  });
});

describe("when the confirm button is on", () => {
  const spec = dialogSpec(
    { kind: "start", comp: "A" },
    act("ready_to_start_a"),
  );
  const ticked = { desk_done: true, estop_hand: true };

  test("only with every check ticked; there is no phrase to type", () => {
    expect(dialogReady(spec, ticked, "", false)).toBe(true);
    expect(dialogReady(spec, { desk_done: true }, "", false)).toBe(false);
    expect(
      dialogReady(spec, { desk_done: true, estop_hand: false }, "", false),
    ).toBe(false);
    expect(dialogReady(spec, {}, "", false)).toBe(false);
  });

  test("a window without checks is on at once", () => {
    const m = dialogSpec({ kind: "start", comp: "M" }, act("idle"));
    expect(dialogReady(m, {}, "", false)).toBe(true);
    const stopAll = dialogSpec({ kind: "sequence_stop" }, act("c_ready"));
    expect(dialogReady(stopAll, {}, "", false)).toBe(true);
  });

  test("the gripper test needs only its tick", () => {
    const grip = dialogSpec(
      { kind: "gripper", action: "open" },
      act("b_ready"),
    );
    expect(dialogReady(grip, {}, "", false)).toBe(false);
    expect(dialogReady(grip, { hands_clear: true }, "", false)).toBe(true);
  });

  test("with a client on C the override phrase is the one text typed, exactly (spaces at the ends do not count)", () => {
    const stop = dialogSpec(
      { kind: "stop", comp: "C" },
      act("c_ready_with_client"),
    );
    const phrase = "stop robot server with client connected";
    expect(dialogReady(stop, {}, "", true)).toBe(false);
    expect(dialogReady(stop, {}, phrase, true)).toBe(true);
    expect(dialogReady(stop, {}, ` ${phrase} `, true)).toBe(true);
    expect(dialogReady(stop, {}, `${phrase}!`, true)).toBe(false);
    expect(dialogReady(stop, {}, phrase.toUpperCase(), true)).toBe(false);
    expect(overrideReady(stop, "", true)).toBe(false);
    // Not asked (no client, nothing refused): nothing to type.
    const plain = dialogSpec({ kind: "stop", comp: "C" }, act("c_ready"));
    expect(overrideReady(plain, "", false)).toBe(true);
    // Asked after the backend refused for a client.
    expect(overrideReady(plain, "", true)).toBe(false);
  });

  test("the request carries every check of the spec as true, and nothing else", () => {
    expect(attestBody(spec)).toEqual({ desk_done: true, estop_hand: true });
    expect(
      attestBody(dialogSpec({ kind: "stop", comp: "C" }, act("c_ready"))),
    ).toEqual({});
    expect(
      attestBody(dialogSpec({ kind: "sequence_stop" }, act("c_ready"))),
    ).toEqual({});
  });
});
