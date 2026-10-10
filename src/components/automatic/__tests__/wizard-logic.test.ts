import { describe, expect, mock, test } from "bun:test";
import {
  ApiError,
  errorOf,
  launchRun,
  newRequestId,
  planCampaign,
} from "../wizard-api";
import { JobAttempts } from "../wizard-job";
import {
  HUMAN_RESET,
  IntentKeys,
  attestAvailable,
  buildCampaignRequest,
  buildJobDraft,
  defaultForm,
  jobName,
  planUsable,
  powerTable,
  resetOptions,
  resetStrategyOf,
  sceneCheckOf,
  shortRunWarning,
  validateCampaign,
  validateChoice,
  validateSettings,
  wholeNumber,
} from "../wizard-logic";
import type { PoliciesResponse, PolicyCheckpoint } from "../wizard-types";

const ck = (
  id: string,
  role: PolicyCheckpoint["role"] = "forward",
): PolicyCheckpoint => ({
  id,
  name: `name-${id}`,
  role,
  config: "cfg",
  sha256_status: "recorded",
  notes: [],
});
const policies = (reset: boolean): PoliciesResponse => ({
  checkpoints: [
    ck("a"),
    ck("b"),
    ck("c"),
    ...(reset ? [ck("r", "reset")] : []),
  ],
  reset_available: reset,
});

describe("reset choices", () => {
  test("with no reset checkpoint only the human reset is offered, and says why", () => {
    const options = resetOptions(policies(false));
    expect(options.resetCheckpoints).toEqual([]);
    expect(options.onlyHumanReasonKey).toBe(
      "automatic.wizard.reset.none_available",
    );
    const form = { ...defaultForm(), policyId: "a", resetChoice: "r" };
    expect(validateChoice(form, policies(false)).reset).toBeTruthy();
  });

  test("reset_available=false hides reset checkpoints even if the list holds one", () => {
    const p = { ...policies(true), reset_available: false };
    expect(resetOptions(p).resetCheckpoints).toEqual([]);
  });

  test("a reset policy is chosen by id and the provider judges the scene", () => {
    const form = { ...defaultForm(), policyId: "a", resetChoice: "r" };
    expect(validateChoice(form, policies(true))).toEqual({});
    expect(resetStrategyOf(form)).toBe("single_reset_policy");
    expect(sceneCheckOf({ ...form, operatorAttested: true })).toBe("provider");
    const draft = buildJobDraft(form, policies(true));
    expect(draft.policy_reset).toEqual({ checkpoint_id: "r" });
  });

  test("the scene is not attested by default, and cannot be while the job has no contract (P1)", () => {
    const form = defaultForm();
    expect(form.resetChoice).toBe(HUMAN_RESET);
    expect(form.operatorAttested).toBe(false);
    expect(attestAvailable()).toBe(false);
    expect(sceneCheckOf(form)).toBe("provider");
    // Even a form that somehow says yes cannot write an attested job: the
    // server refuses operator_attested without task.initial_state_spec.
    expect(sceneCheckOf({ ...form, operatorAttested: true })).toBe("provider");
  });
});

describe("choice and settings checks", () => {
  test("a single run needs a known forward policy", () => {
    expect(validateChoice(defaultForm(), policies(false)).policy).toBeTruthy();
    expect(
      validateChoice({ ...defaultForm(), policyId: "r" }, policies(true))
        .policy,
    ).toBeTruthy();
    expect(
      validateChoice({ ...defaultForm(), policyId: "a" }, policies(false)),
    ).toEqual({});
  });

  test("a campaign needs two to eight arms and a reference among them", () => {
    const base = { ...defaultForm(), mode: "campaign" as const };
    expect(
      validateChoice({ ...base, armIds: ["a"] }, policies(false)).arms,
    ).toBeTruthy();
    expect(
      validateChoice({ ...base, armIds: ["a", "b"] }, policies(false)),
    ).toEqual({});
    expect(
      validateChoice(
        { ...base, armIds: ["a", "b"], referenceId: "c" },
        policies(false),
      ).reference,
    ).toBeTruthy();
  });

  test("settings: instruction, whole-number steps and episodes", () => {
    const ok = { ...defaultForm(), instruction: "put it in" };
    expect(validateSettings(ok)).toEqual({});
    expect(
      validateSettings({ ...ok, instruction: "  " }).instruction,
    ).toBeTruthy();
    expect(validateSettings({ ...ok, maxSteps: "12.5" }).maxSteps).toBeTruthy();
    expect(validateSettings({ ...ok, maxSteps: "0" }).maxSteps).toBeTruthy();
    expect(validateSettings({ ...ok, episodes: "" }).episodes).toBeTruthy();
    expect(wholeNumber("007")).toBe(7);
    expect(wholeNumber("-1")).toBeNull();
  });

  test("fewer than 120 steps is a warning, not an error", () => {
    const f = { ...defaultForm(), instruction: "x", maxSteps: "60" };
    expect(shortRunWarning(f)).toBe(true);
    expect(validateSettings(f)).toEqual({});
    expect(shortRunWarning({ ...f, maxSteps: "400" })).toBe(false);
  });

  test("campaign checks: segment not above trials, alpha in range", () => {
    const f = { ...defaultForm(), mode: "campaign" as const };
    expect(validateCampaign(f)).toEqual({});
    expect(
      validateCampaign({ ...f, segmentTrials: "50" }).segment,
    ).toBeTruthy();
    expect(validateCampaign({ ...f, alpha: "0.9" }).alpha).toBeTruthy();
    expect(validateCampaign({ ...f, trialsPerArm: "1" }).trials).toBeTruthy();
  });
});

describe("requests", () => {
  const form = {
    ...defaultForm(),
    policyId: "a",
    instruction: "  Put the cup in the box  ",
    episodes: "3",
    maxSteps: "250",
  };

  test("the job draft follows the contract's fields", () => {
    expect(buildJobDraft(form, policies(false))).toEqual({
      task: { instruction: "Put the cup in the box" },
      policy_forward: { checkpoint_id: "a" },
      reset: { strategy: "human_assisted", scene_check: "provider" },
      run: { episodes: 3, max_steps: 250 },
      termination: { allow_early_stop: true },
      recording: { group: "name-a" },
    });
  });

  test("a campaign request names every arm, the reference and the schedule", () => {
    const c = {
      ...form,
      mode: "campaign" as const,
      armIds: ["a", "b"],
      referenceId: "a",
    };
    const req = buildCampaignRequest(c, "job-1");
    expect(req.arms).toEqual([
      { id: "A", checkpoint_id: "a", role: "reference" },
      { id: "B", checkpoint_id: "b", role: "candidate" },
    ]);
    expect(req.schedule).toEqual({
      kind: "counterbalanced_segments",
      segment_trials: 5,
      seed: 7,
    });
    expect(req.primary).toEqual({
      metric: "success",
      label_basis: "operator_label",
      alpha: 0.05,
    });
    expect(req.preregistered).toBe(true);
    // The job of a campaign names the reference arm as its forward policy.
    expect(buildJobDraft(c, policies(false)).policy_forward.checkpoint_id).toBe(
      "a",
    );
  });

  test("job names are plain and carry the instruction's readable part", () => {
    const name = jobName("Put the cup!", new Date(2026, 9, 10, 8, 5, 3));
    expect(name).toBe("aeri-20261010-080503-put-the-cup");
    expect(jobName("放杯子", new Date(2026, 9, 10, 8, 5, 3), "x1")).toBe(
      "aeri-20261010-080503-run-x1",
    );
  });

  test("an intent keeps its request id until it is released", () => {
    const keys = new IntentKeys();
    const first = keys.idFor("launch:1");
    expect(keys.idFor("launch:1")).toBe(first);
    expect(keys.idFor("launch:2")).not.toBe(first);
    keys.release("launch:1");
    expect(keys.idFor("launch:1")).not.toBe(first);
  });

  test("a plan is usable only while launchable and unexpired", () => {
    expect(planUsable({ launchable: true, token_expires_at: 2000 }, 1000)).toBe(
      true,
    );
    expect(planUsable({ launchable: true, token_expires_at: 900 }, 1000)).toBe(
      false,
    );
    expect(
      planUsable({ launchable: false, token_expires_at: 2000 }, 1000),
    ).toBe(false);
  });

  test("the power table reads the planner's nested rows (real answer)", () => {
    // Taken from levi.automatic.analysis.power_table([20], [0.5, 0.3]).
    const t = powerTable([
      {
        n: 20,
        wilson_width_at_half: 0.4014039836035753,
        baselines: [
          {
            baseline: 0.5,
            unpaired_fisher: 0.42,
            paired_mcnemar: { "0.0": 0.44, "0.3": 0.39 },
          },
          {
            baseline: 0.3,
            unpaired_fisher: 0.48,
            paired_mcnemar: { "0.0": 0.49, "0.3": 0.42 },
          },
        ],
      },
    ]);
    expect(t.columns).toEqual([
      "n",
      "wilson_width_at_half",
      "baseline",
      "unpaired_fisher",
      "paired_mcnemar@0.0",
      "paired_mcnemar@0.3",
    ]);
    expect(t.cells).toEqual([
      ["20", "0.401", "0.5", "0.42", "0.44", "0.39"],
      ["20", "0.401", "0.3", "0.48", "0.49", "0.42"],
    ]);
    // No cell is ever an object (React error 31 once came of that).
    for (const row of t.cells) for (const c of row) expect(typeof c).toBe("string");
  });

  test("the power table takes whatever columns the planner gives", () => {
    const t = powerTable([
      { n: 20, power: 0.8123456 },
      { n: 30, power: null, note: "x" },
    ]);
    expect(t.columns).toEqual(["n", "power", "note"]);
    expect(t.cells[0]).toEqual(["20", "0.812", "—"]);
    expect(t.cells[1]).toEqual(["30", "—", "x"]);
  });
});

describe("the client", () => {
  const reply = (status: number, body: unknown) =>
    mock(() =>
      Promise.resolve(
        new Response(JSON.stringify(body), {
          status,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    ) as unknown as typeof fetch;

  test("reads the contract's {detail:{code,message,errors}} and the older {error:{…}}", async () => {
    const a = await errorOf(
      new Response(
        JSON.stringify({
          detail: {
            code: "job_invalid",
            message: "bad",
            errors: [{ field: "run.episodes", message: "too big" }, "plain"],
          },
        }),
        { status: 422 },
      ),
    );
    expect(a.status).toBe(422);
    expect(a.code).toBe("job_invalid");
    expect(a.message).toBe("bad");
    expect(a.errors).toEqual([
      { field: "run.episodes", message: "too big" },
      { message: "plain" },
    ]);
    const b = await errorOf(
      new Response(
        JSON.stringify({ error: { code: "E_JOB_EXISTS", message: "m" } }),
        {
          status: 409,
        },
      ),
    );
    expect(b.is("job_exists")).toBe(true);
    const c = await errorOf(
      new Response(JSON.stringify({ detail: "Not found" }), { status: 404 }),
    );
    expect(c.message).toBe("Not found");
    expect(c.code).toBe("HTTP_404");
  });

  test("a launch carries the plan, the token and the confirmation phrase", async () => {
    const original = globalThis.fetch;
    const fetchMock = reply(202, { run_id: "r1" });
    globalThis.fetch = fetchMock;
    try {
      const out = await launchRun(
        "job1",
        { plan_sha256: "p", launch_token: "t" },
        "req-1",
      );
      expect(out.run_id).toBe("r1");
      const [url, init] = (fetchMock as unknown as ReturnType<typeof mock>).mock
        .calls[0] as [string, RequestInit];
      expect(url).toBe("/api/levi/automatic/runs");
      expect(init.method).toBe("POST");
      expect(JSON.parse(init.body as string)).toEqual({
        job_id: "job1",
        plan_sha256: "p",
        launch_token: "t",
        confirm: "launch",
        request_id: "req-1",
      });
      // The page never sends or reads a token header.
      expect(JSON.stringify(init.headers ?? {})).not.toMatch(
        /authorization|token/i,
      );
    } finally {
      globalThis.fetch = original;
    }
  });

  test("a refusal becomes an ApiError with the status", async () => {
    const original = globalThis.fetch;
    globalThis.fetch = reply(412, {
      detail: { code: "plan_changed", message: "changed" },
    });
    try {
      let caught: unknown;
      try {
        await planCampaign({} as never);
      } catch (error) {
        caught = error;
      }
      expect(caught).toBeInstanceOf(ApiError);
      expect((caught as ApiError).status).toBe(412);
    } finally {
      globalThis.fetch = original;
    }
  });

  test("request ids are fresh", () => {
    expect(newRequestId()).not.toBe(newRequestId());
  });
});

describe("making the job", () => {
  const draft = buildJobDraft(
    { ...defaultForm(), policyId: "a", instruction: "go" },
    policies(false),
  );

  test("the same form reuses its job; a changed form makes a new one", async () => {
    let n = 0;
    const api = {
      createJob: mock(async () => ({ id: `job-${++n}` })),
    };
    const attempts = new JobAttempts();
    expect(await attempts.ensure(api, draft)).toBe("job-1");
    expect(await attempts.ensure(api, draft)).toBe("job-1");
    expect(api.createJob).toHaveBeenCalledTimes(1);
    const changed = { ...draft, run: { ...draft.run, episodes: 9 } };
    expect(await attempts.ensure(api, changed)).toBe("job-2");
  });

  test("a taken name is retried with a new name and a new request id", async () => {
    const seen: { name: string; request_id: string }[] = [];
    const api = {
      createJob: mock(async (job: { name: string; request_id: string }) => {
        seen.push({ name: job.name, request_id: job.request_id });
        if (seen.length === 1) throw new ApiError(409, "job_exists", "exists");
        return { id: "job-x" };
      }),
    };
    const id = await new JobAttempts().ensure(api as never, draft);
    expect(id).toBe("job-x");
    expect(seen[1].name).not.toBe(seen[0].name);
    expect(seen[1].request_id).not.toBe(seen[0].request_id);
  });

  test("a 422 reaches the page with its field errors", async () => {
    const api = {
      createJob: async () => {
        throw new ApiError(422, "job_invalid", "bad", [
          { field: "task", message: "empty" },
        ]);
      },
    };
    let caught: unknown;
    try {
      await new JobAttempts().ensure(api as never, draft);
    } catch (error) {
      caught = error;
    }
    expect((caught as ApiError).errors[0].field).toBe("task");
  });
});
