import { describe, expect, test } from "bun:test";
import en from "@/i18n/en.json";
import {
  KEY_BYTE,
  KEY_IDS,
  KEY_LEVELS,
  KEY_MOTIONS,
  KEY_SLOTS,
  KEY_VARIANTS,
  MODES,
  NOTICE_CODES,
  READY_FIELDS,
  READY_LEVELS,
  RUN_FIELDS,
  STATE_IDS,
  STOP_PHASES,
  readyView,
  runView,
  timeOf,
} from "../autonomy-doc";
import {
  CODES,
  READY_VIEWS,
  RUN_VIEWS,
  keyOf,
  readyDoc,
  runDoc,
} from "./autonomy-fixtures";

const KNOWN = new Set(Object.keys(en));
const sorted = (list: readonly string[]) => [...list].sort();

describe("the backend's samples against the page's closed sets", () => {
  test("there is a sample for every state, and the page parses each one", () => {
    expect(RUN_VIEWS.length).toBeGreaterThanOrEqual(30);
    for (const name of RUN_VIEWS) {
      const raw = runDoc(name);
      const view = runView(raw, KNOWN);
      expect([name, view === null]).toEqual([name, false]);
      expect([name, view!.state.id, view!.state.foreign]).toEqual([
        name,
        raw.state.id,
        false,
      ]);
    }
    const covered = new Set(RUN_VIEWS.map((name) => runDoc(name).state.id));
    expect(sorted([...covered])).toEqual(sorted(STATE_IDS));
  });

  test("the top-level fields of every run sample are exactly the page's list", () => {
    for (const name of RUN_VIEWS)
      expect([name, sorted(Object.keys(runDoc(name)))]).toEqual([
        name,
        sorted(RUN_FIELDS),
      ]);
  });

  test("the top-level fields of every readiness sample are exactly the page's list", () => {
    expect(READY_VIEWS.length).toBeGreaterThanOrEqual(3);
    for (const name of READY_VIEWS)
      expect([name, sorted(Object.keys(readyDoc(name)))]).toEqual([
        name,
        sorted(READY_FIELDS),
      ]);
  });

  test("the closed sets equal the backend's aeri_codes.json", () => {
    expect(sorted(STATE_IDS)).toEqual(sorted(CODES.states));
    expect(sorted(KEY_IDS)).toEqual(sorted(CODES.actions));
    expect(sorted(KEY_SLOTS)).toEqual(sorted(CODES.slots));
    expect(sorted(KEY_LEVELS)).toEqual(sorted(CODES.levels));
    expect(sorted(KEY_MOTIONS)).toEqual(sorted(CODES.motions));
    expect(sorted(KEY_VARIANTS)).toEqual(sorted(CODES.variants));
    expect(sorted(STOP_PHASES)).toEqual(sorted(CODES.stop_phases));
    expect(sorted(NOTICE_CODES)).toEqual(sorted(CODES.notices));
  });

  test("every key of every sample is one the page knows, with the byte the page labels it with", () => {
    let seen = 0;
    for (const name of RUN_VIEWS)
      for (const key of runDoc(name).keys) {
        seen += 1;
        expect([
          name,
          key.id,
          KEY_BYTE[key.id as keyof typeof KEY_BYTE],
        ]).toEqual([name, key.id, key.byte]);
        expect(KEY_LEVELS).toContain(key.level);
        expect(KEY_MOTIONS).toContain(key.motion);
        expect(KEY_SLOTS).toContain(key.slot);
        // An allowed:false key always carries its reason.
        if (!key.allowed) expect(key.why_not).not.toBeNull();
      }
    expect(seen).toBeGreaterThan(30);
    // The page keeps every key of the samples (none was dropped as foreign).
    for (const name of RUN_VIEWS)
      expect([name, runView(runDoc(name), KNOWN)!.keys.length]).toEqual([
        name,
        runDoc(name).keys.length,
      ]);
  });

  test("a document of another schema, or no object at all, is not a document", () => {
    for (const bad of [
      null,
      "x",
      [],
      {},
      { ...runDoc("idle"), schema: "levi.marvel0.run.v1" },
    ]) {
      expect(runView(bad, KNOWN)).toBeNull();
      expect(readyView(bad, KNOWN)).toBeNull();
    }
  });
});

describe("what the page refuses to guess", () => {
  test("a state id outside the closed set is `unknown`, said to be foreign", () => {
    const view = runView(
      runDoc("await_release", (doc) => (doc.state.id = "await_telepathy")),
      KNOWN,
    )!;
    expect(view.state.id).toBe("unknown");
    expect(view.state.foreign).toBe(true);
  });

  test("a key outside the closed set is dropped; so is one whose byte is not the one the page labels it with", () => {
    const view = runView(
      runDoc("await_inventory", (doc) => {
        keyOf(doc, "inventory_yes")!.byte = "n";
        doc.keys.push({ ...keyOf(doc, "quit"), id: "fire_everything" });
        doc.keys.push({ ...keyOf(doc, "quit"), id: "exec", level: "Z" });
      }),
      KNOWN,
    )!;
    expect(view.keys.map((key) => key.id)).toEqual(["inventory_no", "quit"]);
  });

  test("a level, motion or slot outside the closed sets drops the key", () => {
    for (const [field, value] of [
      ["level", "D"],
      ["motion", "teleports"],
      ["slot", "third"],
    ]) {
      const view = runView(
        runDoc("await_release", (doc) => {
          keyOf(doc, "release_round")![field] = value;
        }),
        KNOWN,
      )!;
      expect([field, view.keys.map((key) => key.id)]).toEqual([
        field,
        ["quit"],
      ]);
    }
  });

  test("a stop phase outside the closed set is idle, a mode outside it is none", () => {
    const view = runView(
      runDoc("await_release", (doc) => {
        doc.stop.phase = "maybe";
        doc.mode = "telepathy";
      }),
      KNOWN,
    )!;
    expect(view.stop.phase).toBe("idle");
    expect(view.mode).toBeNull();
    expect(MODES).toEqual(["external", "local"]);
  });

  test("a readiness row with a level the page does not know counts as unknown (which refuses)", () => {
    const view = readyView(
      readyDoc("blocked", (doc) => (doc.readiness[0].level = "fine")),
      KNOWN,
    )!;
    expect(view.rows[0].level).toBe("unknown");
    expect(READY_LEVELS).toContain(view.rows[0].level);
  });

  test("a prepared run that does not say it is intact is not treated as intact", () => {
    const view = readyView(
      readyDoc("prepared_ready", (doc) => delete doc.prepared.intact),
      KNOWN,
    )!;
    expect(view.prepared?.intact).toBe(false);
  });

  test("an off-by-type value is null, never a guess", () => {
    const view = runView(
      runDoc("forward_running", (doc) => {
        doc.progress.step = "66";
        doc.state.seq = "11";
        doc.queue.pending = -3;
      }),
      KNOWN,
    )!;
    expect(view.progress?.step).toBeNull();
    expect(view.state.seq).toBe(-1);
    expect(view.queue?.pending).toBe(0);
  });
});

describe("what the page takes from a document", () => {
  test("times: ISO with an offset and a fraction of a second", () => {
    expect(timeOf("2026-10-21T10:31:06.5+08:00")).toBe(
      Date.parse("2026-10-21T02:31:06.500Z"),
    );
    expect(timeOf("not a time")).toBeNull();
    expect(timeOf(null)).toBeNull();
  });

  test("a key: its byte, level, slot, token, delay and phrase", () => {
    const view = runView(runDoc("await_phrase"), KNOWN)!;
    const [execute] = view.keys;
    expect(execute).toMatchObject({
      id: "execute",
      byte: "execute",
      level: "C",
      motion: "may_move",
      slot: "first",
      allowed: true,
      phrase: "execute aeri-20261021-ext-r01",
    });
    expect(execute.token).toBe(keyOf(runDoc("await_phrase"), "execute")!.token);
    expect(execute.notBefore).toBe(Date.parse("2026-10-21T10:31:06.5+08:00"));
  });

  test("the reason a key is off, in the page's words when it has them", () => {
    const view = runView(runDoc("await_release_key_sent"), KNOWN)!;
    const [release] = view.keys;
    expect(release.allowed).toBe(false);
    expect(release.whyCode).toBe("key_sent");
    expect(release.whyNot?.say).toEqual({
      key: "marvel0.autonomy.err.key_sent",
    });
    const foreign = runView(
      runDoc("await_release_key_sent", (doc) => {
        doc.keys[0].why_not = { code: "from_the_future", params: {} };
      }),
      KNOWN,
    )!;
    // A code the page has no sentence for shows the server's own, marked so.
    expect(foreign.keys[0].whyNot?.say).toBeNull();
    expect(foreign.keys[0].whyNot?.text).toBe("from_the_future");
  });

  test("the state's reason takes its facts from the sibling params", () => {
    const stopping = runView(runDoc("stopping_unconfirmed"), KNOWN)!;
    expect(stopping.state.reason?.say).toEqual({
      key: "marvel0.autonomy.reason.stop_sent",
      params: { since_stop_s: 12 },
    });
  });

  test("the prompt carries the line D printed, verbatim", () => {
    const view = runView(runDoc("await_inventory"), KNOWN)!;
    expect(view.state.prompt?.line).toContain("清单对就按 y 确认");
    expect(view.state.prompt?.kind).toBe("inventory");
  });

  test("the exit of D: code and signal", () => {
    const view = runView(runDoc("ended_hangup"), KNOWN)!;
    // The hang-up chain's SIGKILL: no exit code, signal 9.
    expect(view.d.exit).toBeNull();
    expect(view.d.exitSignal).toBe(9);
    expect(runView(runDoc("ended_refused"), KNOWN)!.d.exit).toBe(2);
    expect(view.afterExit?.units).toEqual([
      "levi-aeri-vlm-8100",
      "levi-aeri-policy-forward",
    ]);
    expect(view.afterExit?.swapClean).toBe(false);
    expect(view.afterExit?.ports?.map((p) => p.port)).toEqual(["8000", "8100"]);
  });

  test("the readiness document: rows, limits, prepared run, phrases", () => {
    const view = readyView(readyDoc("prepared_ready"), KNOWN)!;
    expect(view.enabled).toBe(true);
    expect(view.site.state).toBe("ok");
    expect(view.rows.length).toBeGreaterThan(15);
    expect(view.limits?.steps.max).toBe(3000);
    expect(view.limits?.rounds).toEqual({ min: 1, max: 50, default: 5 });
    expect(view.prepared?.name).toMatch(/^aeri-/);
    expect(view.prepared?.intact).toBe(true);
    expect(view.prepared?.execute).toContain("aeri.py execute");
    expect(view.confirm).toEqual({
      prepare: "prepare aeri run",
      liveStop: "stop live service",
      leftovers: "stop leftover aeri units",
    });
  });

  test("an unconfigured site and a control file without the section", () => {
    const view = readyView(readyDoc("unconfigured"), KNOWN)!;
    expect(view.enabled).toBe(false);
    expect(view.controlState).toBe("missing");
    expect(view.site.state).toBe("unconfigured");
    expect(view.profile).toBeNull();
    expect(view.limits).toBeNull();
  });
});
