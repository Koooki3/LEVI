import { describe, expect, test } from "bun:test";
import { readdirSync, readFileSync } from "fs";
import { join } from "path";
import en from "@/i18n/en.json";
import { sayRefusal, RUN_REFUSE, STACK_REFUSE } from "../ctl-failure";
import { blockersOf, codedSay, paramsOf, stopping, wordOf } from "../ctl-text";
import { errorOf } from "../deploy-api";
import * as runApi from "../run-api";
import * as stackApi from "../stack-api";

const KNOWN = new Set(Object.keys(en));

/** A fetch that records what it was asked and answers 200 with {}. */
function recorder() {
  const seen: Array<{ url: string; init?: RequestInit }> = [];
  const fetchFn = async (url: string, init?: RequestInit) => {
    seen.push({ url, init });
    return new Response("{}", { status: 200 });
  };
  return { seen, fetchFn };
}

describe("the arm stack's routes", () => {
  test("each call goes to its own same-origin route; a write is one JSON POST", async () => {
    const { seen, fetchFn } = recorder();
    const api = stackApi.createStackApi({ fetchFn });
    await api.read();
    await api.start("A", { request_id: "x".repeat(8) });
    await api.stop("C", { request_id: "x".repeat(8) });
    await api.marker({});
    await api.preflight("A", {});
    await api.preflight("C", {});
    await api.gripper({});
    await api.sequence({});
    await api.sequenceCancel({});
    await api.log("B", 40);
    expect(seen.map((s) => `${s.init?.method ?? "GET"} ${s.url}`)).toEqual([
      "GET /api/levi/marvel0/stack",
      "POST /api/levi/marvel0/stack/a/start",
      "POST /api/levi/marvel0/stack/c/stop",
      "POST /api/levi/marvel0/stack/marker",
      "POST /api/levi/marvel0/stack/a/preflight",
      "POST /api/levi/marvel0/stack/c/preflight",
      "POST /api/levi/marvel0/stack/b/gripper",
      "POST /api/levi/marvel0/stack/sequence/start",
      "POST /api/levi/marvel0/stack/sequence/cancel",
      "GET /api/levi/marvel0/stack/b/log?lines=40",
    ]);
    for (const call of seen) {
      expect(call.init?.cache).toBe("no-store");
      const headers = (call.init?.headers ?? {}) as Record<string, string>;
      // Nothing here reads or sends a token or a credential.
      expect(
        Object.keys(headers).filter((h) => h.toLowerCase() !== "content-type"),
      ).toEqual([]);
    }
  });

  test("the request bodies are closed: exactly the fields the backend takes", () => {
    expect(
      stackApi.startBody("id", "start arm stack", { desk_done: true }),
    ).toEqual({
      request_id: "id",
      confirm: "start arm stack",
      attest: { desk_done: true },
    });
    expect(stackApi.stopBody("id", "stop robot server", "")).toEqual({
      request_id: "id",
      confirm: "stop robot server",
      client_override: "",
    });
    expect(stackApi.markerBody("id", "adopt", "adopt marker")).toEqual({
      request_id: "id",
      op: "adopt",
      confirm: "adopt marker",
    });
    expect(stackApi.preflightBody("id")).toEqual({ request_id: "id" });
    expect(
      stackApi.gripperBody("id", "open", "gripper open", { hands_clear: true }),
    ).toEqual({
      request_id: "id",
      action: "open",
      confirm: "gripper open",
      attest: { hands_clear: true },
    });
    expect(stackApi.sequenceBody("id", "start arm stack", {})).toEqual({
      request_id: "id",
      confirm: "start arm stack",
      attest: {},
    });
  });
});

describe("the hosted client's routes", () => {
  test("each call goes to its own route", async () => {
    const { seen, fetchFn } = recorder();
    const api = runApi.createRunApi({ fetchFn });
    await api.read();
    await api.challenge({});
    await api.start({});
    await api.key({});
    await api.hold({});
    await api.end({});
    await api.log();
    expect(seen.map((s) => `${s.init?.method ?? "GET"} ${s.url}`)).toEqual([
      "GET /api/levi/marvel0/run",
      "POST /api/levi/marvel0/run/challenge",
      "POST /api/levi/marvel0/run/start",
      "POST /api/levi/marvel0/run/key",
      "POST /api/levi/marvel0/run/hold",
      "POST /api/levi/marvel0/run/end",
      "GET /api/levi/marvel0/run/log",
    ]);
  });

  test("the bodies: a key names an action and carries a challenge, never a raw key", () => {
    expect(runApi.keyBody("id", "enter", "tok")).toEqual({
      request_id: "id",
      action: "enter",
      challenge: "tok",
    });
    expect(
      runApi.keyBody("id", "exit_home", "tok", "exit and go home"),
    ).toEqual({
      request_id: "id",
      action: "exit_home",
      challenge: "tok",
      confirm: "exit and go home",
    });
    expect(runApi.holdBody("id")).toEqual({ request_id: "id" });
    expect(runApi.endBody("id", "end client run")).toEqual({
      request_id: "id",
      confirm: "end client run",
    });
    // The backend's models are closed (levi/marvel0/run_api.py ChallengeBody,
    // StartBody): a challenge has no request id and a start carries `attest`.
    expect(runApi.challengeBody("label")).toEqual({ purpose: "label" });
    expect(runApi.challengeBody("start", { a: 1 })).toEqual({
      purpose: "start",
      form: { a: 1 },
    });
    expect(
      runApi.startRunBody("id", { a: 1 }, "tok", "start client g", {
        estop: true,
        scene_reset: true,
        area_clear: true,
      }),
    ).toEqual({
      request_id: "id",
      form: { a: 1 },
      challenge: "tok",
      confirm: "start client g",
      attest: { estop: true, scene_reset: true, area_clear: true },
    });
  });
});

/** The fields of the closed body models of a backend route file
 * (`class X(Closed):` with `name: type` lines): all, and the required ones. */
function bodyModels(file: string) {
  const text = readFileSync(
    join(import.meta.dir, "../../../../levi/marvel0", file),
    "utf8",
  );
  const models: Record<string, { all: string[]; required: string[] }> = {};
  for (const block of text.split(/^class /m).slice(1)) {
    const name = block.slice(0, block.indexOf("("));
    if (!/^[A-Za-z]+\(Closed\):/.test(block)) continue;
    const fields = [...block.matchAll(/^ {4}([a-z_]+): ([^\n]+)$/gm)];
    models[name] = {
      all: fields.map((f) => f[1]),
      required: fields.filter((f) => !f[2].includes("=")).map((f) => f[1]),
    };
  }
  return models;
}

/** A body the page sends is exactly a body the backend accepts: its models
 * are closed (an unknown key, or a missing required one, is a 422), and the
 * page's own fakes would never notice a field that was renamed. */
function accepts(model: { all: string[]; required: string[] }, body: object) {
  const keys = Object.keys(body);
  return {
    unknown: keys.filter((key) => !model.all.includes(key)),
    missing: model.required.filter((key) => !keys.includes(key)),
  };
}
const OK = { unknown: [], missing: [] };

describe("the bodies the page sends are the ones the backend's closed models take", () => {
  const confirmAll = { estop: true, scene_reset: true, area_clear: true };

  test("the client's routes (levi/marvel0/run_api.py)", () => {
    const m = bodyModels("run_api.py");
    expect(Object.keys(m)).toEqual(
      expect.arrayContaining([
        "ChallengeBody",
        "StartBody",
        "KeyBody",
        "HoldBody",
        "EndBody",
        "Attest",
      ]),
    );
    expect(accepts(m.ChallengeBody, runApi.challengeBody("enter"))).toEqual(OK);
    expect(
      accepts(m.ChallengeBody, runApi.challengeBody("start", { a: 1 })),
    ).toEqual(OK);
    expect(
      accepts(
        m.StartBody,
        runApi.startRunBody("r", { a: 1 }, "t", "c", confirmAll),
      ),
    ).toEqual(OK);
    expect(
      accepts(
        m.Attest,
        runApi.startRunBody("r", {}, "t", "c", confirmAll).attest as object,
      ),
    ).toEqual(OK);
    expect(accepts(m.KeyBody, runApi.keyBody("r", "enter", "t"))).toEqual(OK);
    expect(
      accepts(m.KeyBody, runApi.keyBody("r", "exit_home", "t", "c")),
    ).toEqual(OK);
    expect(accepts(m.HoldBody, runApi.holdBody("r"))).toEqual(OK);
    expect(accepts(m.EndBody, runApi.endBody("r", "c"))).toEqual(OK);
  });

  test("the arm stack's routes (levi/marvel0/stack_act_api.py)", () => {
    const m = bodyModels("stack_act_api.py");
    expect(Object.keys(m)).toEqual(
      expect.arrayContaining([
        "CheckBody",
        "StartBody",
        "StopBody",
        "MarkerBody",
        "GripperBody",
      ]),
    );
    expect(
      accepts(m.StartBody, stackApi.startBody("r", "c", { hands_clear: true })),
    ).toEqual(OK);
    expect(accepts(m.StopBody, stackApi.stopBody("r", "c", ""))).toEqual(OK);
    expect(
      accepts(m.MarkerBody, stackApi.markerBody("r", "touch", "c")),
    ).toEqual(OK);
    expect(accepts(m.CheckBody, stackApi.preflightBody("r"))).toEqual(OK);
    expect(
      accepts(
        m.GripperBody,
        stackApi.gripperBody("r", "open", "c", { hands_clear: true }),
      ),
    ).toEqual(OK);
    // The guided start takes the start model; its cancel takes the check model.
    expect(accepts(m.StartBody, stackApi.sequenceBody("r", "c", {}))).toEqual(
      OK,
    );
  });

  test("the check itself notices a field that was renamed", () => {
    const m = bodyModels("run_api.py");
    expect(
      accepts(m.StartBody, {
        ...runApi.startRunBody("r", {}, "t", "c", confirmAll),
        ack: {},
      }).unknown,
    ).toEqual(["ack"]);
    expect(
      accepts(m.ChallengeBody, { request_id: "r", purpose: "start" }).unknown,
    ).toEqual(["request_id"]);
    expect(accepts(m.EndBody, { request_id: "r" }).missing).toEqual([
      "confirm",
    ]);
  });
});

describe("nothing of the control page reaches a robot-side port", () => {
  test("no source of the arm stack's or the client's controls names a robot or policy port or a loopback address", () => {
    const dir = join(import.meta.dir, "..");
    const files = readdirSync(dir).filter((f) =>
      /^(stack-(api|act-logic|controls|dialog|dialog-logic|panel|evidence)|run-(api|logic|panel|start)|use-(stack|run|polled)|ctl-|chain-|selection|live-age|notice)/.test(
        f,
      ),
    );
    expect(files.length).toBeGreaterThan(15);
    for (const file of files) {
      const code = readFileSync(join(dir, file), "utf8");
      expect([file, code.match(/\b(?:5000|5001|5100|7470|8000)\b/)]).toEqual([
        file,
        null,
      ]);
      expect([file, code.match(/127\.0\.0\.1|localhost|https?:\/\//)]).toEqual([
        file,
        null,
      ]);
    }
  });
});

describe("a refusal in words", () => {
  const failure = (code: string, extra: Record<string, unknown> = {}) =>
    errorOf(409, {
      detail: { code, message: "The server's English sentence", ...extra },
    });

  test("the stack's code is worded with its facts", () => {
    const said = sayRefusal(
      failure("gate.layers_known", { unknown: "B" }),
      STACK_REFUSE,
      KNOWN,
    );
    expect(said.say).toEqual({
      key: "marvel0.ctl.block.gate.layers_known",
      params: { unknown: "B" },
    });
  });

  test("a monitor code of the S4-0 card is found in its family", () => {
    expect(
      sayRefusal(failure("monitor_running"), STACK_REFUSE, KNOWN).say,
    ).toEqual({
      key: "marvel0.stack.refuse.monitor_running",
    });
  });

  test("the run's refusals and the reasons an action is not offered", () => {
    expect(
      sayRefusal(failure("challenge_expired"), RUN_REFUSE, KNOWN).say?.[
        "key" as never
      ],
    ).toBe("marvel0.run.refuse.challenge_expired");
    expect(sayRefusal(failure("enter_locked"), RUN_REFUSE, KNOWN).say).toEqual({
      key: "marvel0.run.why.enter_locked",
    });
  });

  test("an unknown code, or a known one whose facts did not come, is the server's sentence", () => {
    expect(sayRefusal(failure("brand_new"), STACK_REFUSE, KNOWN)).toEqual({
      say: null,
      text: "The server's English sentence",
    });
    expect(
      sayRefusal(failure("gate.layers_known"), STACK_REFUSE, KNOWN).say,
    ).toBeNull();
  });

  test("no answer at all is said as such: the write may have arrived", () => {
    const none = errorOf(0, {});
    none.code = "network";
    expect(sayRefusal(none, STACK_REFUSE, KNOWN).say).toEqual({
      key: "marvel0.err.network_write",
    });
  });
});

describe("coded text", () => {
  test("params keep numbers and short texts and drop anything else", () => {
    expect(paramsOf({ a: 1, b: "two", c: { d: 1 }, e: [1], f: null })).toEqual({
      a: 1,
      b: "two",
    });
  });

  test("blockers: bare codes and objects; a warning does not stop; the same code is listed once", () => {
    const list = blockersOf(
      [
        "stack_running",
        { code: "gate.linger", level: "warn", text: "t", params: {} },
        { code: "stack_running", level: "required", text: "again" },
        { nope: true },
      ],
      KNOWN,
    );
    expect(list.map((b) => b.code)).toEqual(["stack_running", "gate.linger"]);
    expect(stopping(list).map((b) => b.code)).toEqual(["stack_running"]);
    expect(list[0].worded.say).toEqual({
      key: "marvel0.stack.refuse.stack_running",
    });
  });

  test("a code is worded by the first family that has it", () => {
    expect(
      codedSay(
        ["x.", "marvel0.stack.refuse."],
        "monitor_running",
        "t",
        {},
        KNOWN,
      ).say,
    ).toEqual({
      key: "marvel0.stack.refuse.monitor_running",
    });
    expect(wordOf((k) => `T(${k})`, "marvel0.ctl.state.", "ready")).toBe(
      "T(marvel0.ctl.state.ready)",
    );
    expect(wordOf((k) => `T(${k})`, "marvel0.ctl.state.", "mystery")).toBe(
      "mystery",
    );
  });
});
