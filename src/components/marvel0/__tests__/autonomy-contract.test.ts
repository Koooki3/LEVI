import { describe, expect, test } from "bun:test";
import { readFileSync } from "fs";
import { join } from "path";
import {
  AUTONOMY_DEADLINES,
  challengeBody,
  confirmBody,
  createAutonomyApi,
  endBody,
  keyBody,
  presenceBody,
  startBody,
  stopBody,
} from "../autonomy-api";
import {
  KEY_BYTE,
  KEY_IDS,
  KEY_LEVELS,
  KEY_MOTIONS,
  KEY_SLOTS,
  STATE_IDS,
} from "../autonomy-doc";
import { CODES } from "./autonomy-fixtures";
import { LIMITS, prepareBody, requiredAttest } from "../autonomy-logic";
import en from "@/i18n/en.json";

// The page against the backend's own source: its routes, the closed models of
// its bodies, its constants and its key table are read as text from
// levi/marvel0/ (the way ctl-catalog.test.ts reads the stack's tables), so a
// change on either side fails here and not on the robot.

const root = join(import.meta.dir, "../../../..");
const backend = (file: string) =>
  readFileSync(join(root, "levi/marvel0", file), "utf8");
const run = backend("aeri_run_api.py");
const prep = backend("aeri_api.py");
const state = backend("aeri_state.py");

/** The fields of the models of one python file: `class Name(Closed):` with its
 * annotated lines. */
function models(source: string): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  const starts = [
    ...source.matchAll(/^class (\w+)\((?:Closed|BaseModel)\):\n/gm),
  ];
  starts.forEach((match, i) => {
    const end = starts[i + 1]?.index ?? source.length;
    const body = source.slice(match.index! + match[0].length, end);
    out[match[1]] = [...body.matchAll(/^    (\w+): /gm)]
      .map((m) => m[1])
      .filter((name) => name !== "model_config");
  });
  return out;
}
const M = { ...models(run), ...models(prep) };

describe("the routes", () => {
  test("the page calls exactly the twelve routes the two routers define", async () => {
    const backendRoutes = [
      ...(run + prep).matchAll(/@router\.(get|post)\("([^"]+)"/g),
    ]
      .map((m) => `${m[1].toUpperCase()} ${m[2]}`)
      .sort();
    expect(backendRoutes).toHaveLength(12);
    const seen: string[] = [];
    const api = createAutonomyApi({
      fetchFn: async (input, init) => {
        const path = String(input)
          .replace("/api/levi/marvel0", "")
          .split("?")[0];
        seen.push(`${init?.method ?? "GET"} ${path}`);
        return new Response("{}", { status: 200 });
      },
    });
    await api.ready();
    await api.prepare({});
    await api.liveStop({});
    await api.leftoversStop({});
    await api.run();
    await api.challenge({});
    await api.start({});
    await api.key({});
    await api.stop({});
    await api.end({});
    await api.presence({});
    await api.log(80);
    expect(seen.sort()).toEqual(backendRoutes);
  });

  test("the lines asked of the log are one to 200, as the route holds them", async () => {
    const urls: string[] = [];
    const api = createAutonomyApi({
      fetchFn: async (input) => {
        urls.push(String(input));
        return new Response("{}");
      },
    });
    await api.log(0);
    await api.log(9999);
    expect(urls.map((u) => u.split("lines=")[1])).toEqual(["1", "200"]);
    expect(run).toContain("max(1, min(int(lines), 200))");
  });

  test("every write has a time to wait that fits what the backend does (a prepare runs a check of up to a minute)", () => {
    expect(AUTONOMY_DEADLINES.prepare).toBeGreaterThan(60_000);
    expect(AUTONOMY_DEADLINES.start).toBeGreaterThanOrEqual(30_000);
    expect(AUTONOMY_DEADLINES.write).toBeLessThanOrEqual(15_000);
  });
});

describe("the bodies are the closed models of the routes", () => {
  const keys = (body: Record<string, unknown>) => Object.keys(body).sort();
  const fields = (name: string) => [...M[name]].sort();

  test("every model of the routes is closed and strict", () => {
    expect(run).toContain(
      'model_config = ConfigDict(extra="forbid", strict=True)',
    );
    expect(prep).toContain(
      'model_config = ConfigDict(extra="forbid", strict=True)',
    );
  });

  test("challenge", () => {
    expect(keys(challengeBody("n"))).toEqual(fields("ChallengeBody"));
    expect(challengeBody("n").purpose).toBe("start");
  });

  test("start, with the four checks the backend names", () => {
    const attest = {
      estop: true,
      scene_ok: true,
      area_clear: true,
      arm_home: true,
    };
    const body = startBody("req-12345678", "n", "tok", "start aeri n", attest);
    expect(keys(body)).toEqual(fields("StartBody"));
    expect(keys(body.attest as Record<string, unknown>)).toEqual(
      fields("Attest"),
    );
  });

  test("key, with `seen`, the token and the tab", () => {
    const seen = { run_id: "r", state_seq: 3, prompt_id: "p:1" };
    const body = keyBody("req-12345678", "quit", seen, "t", "tab");
    const optional = ["confirm", "attest"];
    expect(keys(body).filter((k) => !optional.includes(k))).toEqual(
      fields("KeyBody").filter((k) => !optional.includes(k)),
    );
    expect(keys(body.seen as Record<string, unknown>)).toEqual(fields("Seen"));
    // `confirm` (a level C key only) and `attest` (a key with ticks only) are
    // the two optional fields.
    expect(M.KeyBody).toContain("confirm");
    expect(M.KeyBody).toContain("attest");
    expect(
      keys(keyBody("req-12345678", "execute", seen, "t", "tab", "execute n")),
    ).toContain("confirm");
    expect(
      keys(
        keyBody("req-12345678", "release_round", seen, "t", "tab", undefined, [
          "scene_ready",
        ]),
      ),
    ).toEqual(["action", "attest", "request_id", "seen", "tab_id", "token"]);
    expect(keys(body)).not.toContain("confirm");
    expect(keys(body)).not.toContain("attest");
    // `prompt_id` is a string in the backend's model, not an optional one.
    expect(run).toMatch(/class Seen\(Closed\):[\s\S]*?prompt_id: str\n/);
  });

  test("the actions of the key body are the actions the page has, no more and no fewer", () => {
    const literal = run.match(/action: Literal\[([\s\S]*?)\]/)![1];
    const actions = [...literal.matchAll(/"(\w+)"/g)].map((m) => m[1]);
    expect([...actions].sort()).toEqual([...KEY_IDS].sort());
  });

  test("stop has nothing but its request id: no seen, no token", () => {
    expect(keys(stopBody("req-12345678"))).toEqual(fields("StopBody"));
    expect(fields("StopBody")).toEqual(["request_id"]);
  });

  test("end, presence, live stop and leftovers stop", () => {
    expect(
      keys(endBody("req-12345678", "end terminal D", requiredAttest("end"))),
    ).toEqual(fields("EndBody"));
    expect(fields("EndBody")).toContain("attest");
    expect(keys(presenceBody("tab", true))).toEqual(fields("PresenceBody"));
    expect(keys(confirmBody("req-12345678", "x"))).toEqual(
      fields("ConfirmBody"),
    );
  });

  test("prepare: the fields of the model, whole numbers where the model says int, a number or null for the gripper wait", () => {
    const body = prepareBody("req-12345678", {
      mode: "local",
      task: "pick the pepper",
      rounds: "5",
      steps: "200",
      gripperConfirmS: "",
      startHome: true,
    });
    expect(keys(body)).toEqual(fields("PrepareBody"));
    expect(prep).toMatch(
      /rounds: int\n\s+steps: int\n\s+gripper_confirm_s: float \| None = None/,
    );
    expect(Number.isInteger(body.rounds)).toBe(true);
    expect(Number.isInteger(body.steps)).toBe(true);
    expect(body.gripper_confirm_s).toBeNull();
    expect(prep).toMatch(/mode: Literal\["external", "local"\]/);
  });
});

/** `aeri_state.KEY_ATTEST` as the Python source writes it: the named tuples,
 * then the table that spreads them. */
function backendAttest(): Record<string, string[]> {
  const tuples: Record<string, string[]> = {};
  for (const m of state.matchAll(/^([A-Z]+_ATTEST) = \(([^)]*)\)/gm))
    tuples[m[1]] = [...m[2].matchAll(/"(\w+)"/g)].map((x) => x[1]);
  const table = state.match(/^KEY_ATTEST = \{([\s\S]*?)\n\}/m)![1];
  const out: Record<string, string[]> = {};
  for (const line of table.split("\n")) {
    const entry = line.match(/^\s+"(\w+)": (.+?),?$/);
    if (!entry) continue; // a comment, or the blank line
    const value = entry[2];
    const spread = [...value.matchAll(/\*([A-Z_]+)/g)];
    out[entry[1]] = spread.length
      ? spread.flatMap((x) => tuples[x[1]])
      : /^[A-Z_]+$/.test(value)
        ? tuples[value]
        : [...value.matchAll(/"(\w+)"/g)].map((x) => x[1]);
  }
  return out;
}

describe("the ticks of a window are the table the backend holds the key to (SAFETY N6)", () => {
  const table = backendAttest();

  test("the backend names the keys that have ticks: the page has ticks for exactly those", () => {
    expect(Object.keys(table).sort()).toEqual([
      "end",
      "execute",
      "execute_variant",
      "release_round",
    ]);
    const withTicks = [...KEY_IDS, "end" as const].filter(
      (id) => requiredAttest(id).length > 0,
    );
    expect([...withTicks].sort()).toEqual(Object.keys(table).sort());
  });

  test("the codes of each, in order, are the ones the page sends in `attest`", () => {
    for (const [id, codes] of Object.entries(table))
      expect([id, requiredAttest(id as never)]).toEqual([id, codes]);
  });

  test("the codes file the backend generates says the same", () => {
    expect(CODES.key_attest).toEqual(table);
  });

  test("the route's closed list of ticks is the union of those, no more", () => {
    const literal = run.match(/Tick = Literal\[([\s\S]*?)\]/)![1];
    const listed = [...literal.matchAll(/"(\w+)"/g)].map((m) => m[1]);
    const union = [...new Set(Object.values(table).flat())];
    expect([...listed].sort()).toEqual([...union].sort());
  });

  test("a key that has ticks is refused by the page's body builder with fewer, and the backend names the refusal the page words", () => {
    expect(state).toContain('"attest_incomplete"');
    expect(typeof en["marvel0.autonomy.err.attest_incomplete"]).toBe("string");
  });
});

describe("the constants of the backend", () => {
  const number = (name: string) =>
    Number(state.match(new RegExp(`^${name} = ([0-9.]+)`, "m"))![1]);

  test("the delay after a key appears, the gap after a key, and the time a key may stay unseen are the backend's", () => {
    expect(LIMITS.armDelayMs).toBe(number("ARM_DELAY_S") * 1000);
    expect(LIMITS.keyGapMs).toBe(number("KEY_GAP_S") * 1000);
    expect(LIMITS.sentUnseenMs).toBe(number("KEY_NOT_TAKEN_S") * 1000);
  });

  test("the presence beat is much shorter than the time the backend lets a tab be silent", () => {
    const silent = number("PRESENCE_S") * 1000;
    expect(silent).toBe(15_000);
    expect(LIMITS.presenceMs * 2).toBeLessThan(silent);
    // The stop acknowledgement window is the one the Stop caption names.
    expect(number("STOP_ACK_S")).toBe(5);
  });

  test("the page reads D's state at least as often as the backend caches it", () => {
    expect(LIMITS.runPollMs).toBeLessThanOrEqual(1000);
    expect(LIMITS.docStaleMs).toBe(2000);
  });

  test("the keys: the byte, level, motion and slot of every action are the backend's table", () => {
    const rows = [
      ...state.matchAll(
        /KeySpec\(\s*"(\w+)",\s*"([^"]+)",\s*(?:b"[^"]*"|None|ENTER),\s*"([ABC])",\s*"(\w+)",\s*"(\w+)"/g,
      ),
    ];
    expect(rows.map((m) => m[1]).sort()).toEqual([...KEY_IDS].sort());
    for (const [, id, label, level, motion, slot] of rows) {
      expect([id, label]).toEqual([id, KEY_BYTE[id as keyof typeof KEY_BYTE]]);
      expect(KEY_LEVELS).toContain(level);
      expect(KEY_MOTIONS).toContain(motion);
      expect(KEY_SLOTS).toContain(slot);
    }
  });

  test("the states and the bytes the page can ever cause", () => {
    const states = state.match(/\nSTATES = \(([\s\S]*?)\n\)/)![1];
    expect([...states.matchAll(/"(\w+)"/g)].map((m) => m[1]).sort()).toEqual(
      [...STATE_IDS].sort(),
    );
    // The only bytes: y n o p h s f d q, Enter and Ctrl+C. Never Ctrl+D, ESC,
    // a capital or a digit.
    const wires = [...state.matchAll(/KeySpec\([^)]*?(b"([^"]*)"|ENTER)/g)].map(
      (m) => m[2] ?? "\r",
    );
    expect(new Set(wires)).toEqual(
      new Set(["y", "n", "o", "p", "h", "s", "f", "d", "q", "\r"]),
    );
    expect(state).toContain('STOP_BYTE = b"\\x03"');
    expect(state).not.toMatch(/b"\\x04"|b"\\x1b"/);
  });
});
