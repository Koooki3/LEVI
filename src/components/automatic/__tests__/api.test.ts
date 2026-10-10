import { afterEach, describe, expect, test } from "bun:test";
import { AutomaticApiError, automaticApi, frameUrl } from "../api";
import { mockFetch, plan } from "./fixtures";

let restore = () => {};
afterEach(() => restore());

describe("the automatic API client", () => {
  test("a launch sends the plan digest, the token, the phrase and one request id", async () => {
    const m = mockFetch({ "POST runs": () => ({ run_id: "run-new" }) });
    restore = m.restore;
    await automaticApi.launch(plan(), "job-1", "req-1");
    expect(m.calls[0].body).toEqual({
      job_id: "job-1",
      plan_sha256: "p".repeat(64),
      launch_token: "tok",
      confirm: "launch",
      request_id: "req-1",
    });
  });
  test("resume maps to the contract's snake_case body", async () => {
    const m = mockFetch({
      "POST runs/run-1/resume": () => ({ result: "applied" }),
    });
    restore = m.restore;
    await automaticApi.resume("run-1", {
      commandId: "c1",
      expectedSeq: 41,
      environmentHandled: true,
      healthRechecked: true,
      challenge: "chal",
    });
    expect(m.calls[0].body).toEqual({
      command_id: "c1",
      expected_seq: 41,
      environment_handled: true,
      health_rechecked: true,
      challenge: "chal",
    });
  });
  test("the error shapes of the server are read: detail object, error object, refused command", async () => {
    for (const body of [
      { detail: { code: "plan_changed", message: "changed" } },
      { error: { code: "plan_changed", message: "changed" } },
      { result: "refused", code: "plan_changed", message: "changed" },
    ]) {
      const m = mockFetch({ "POST plan": () => ({ __status: 412, body }) });
      restore = m.restore;
      const caught = await automaticApi.plan("j").catch((e) => e);
      m.restore();
      expect(caught).toBeInstanceOf(AutomaticApiError);
      expect(caught.status).toBe(412);
      expect(caught.code).toBe("plan_changed");
      expect(caught.message).toBe("changed");
    }
  });
  test("a body that is not JSON keeps the status line", async () => {
    const original = globalThis.fetch;
    globalThis.fetch = (async () =>
      new Response("oops", { status: 502 })) as typeof fetch;
    restore = () => {
      globalThis.fetch = original;
    };
    const caught = await automaticApi.runs().catch((e) => e);
    expect(caught.message).toBe("HTTP 502");
    expect(caught.code).toBe("");
  });
  test("the label call sends only the episode and the value", async () => {
    const m = mockFetch({
      "POST runs/run-1/labels": () => ({
        label: "success",
        revealed: true,
        card: null,
      }),
    });
    restore = m.restore;
    await automaticApi.label("run-1", "ep-1", "success");
    expect(m.calls[0].body).toEqual({ episode_id: "ep-1", value: "success" });
  });
  test("a frame URL is built only from a plain digest", () => {
    expect(frameUrl("run-1", "a".repeat(64))).toBe(
      `/api/levi/automatic/runs/run-1/frames/${"a".repeat(64)}`,
    );
    expect(frameUrl("run-1", "../../etc/passwd")).toBeNull();
    expect(frameUrl("run-1", "a/b")).toBeNull();
  });
});
