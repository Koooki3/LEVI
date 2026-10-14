import { render, setupDom } from "@/components/ds/__tests__/dom";
import { describe, expect, test } from "bun:test";
import { requirements } from "./fixtures";
import { RequirementsCard, hasRequirements } from "../requirements-card";
import { stepProgressText } from "../run-logic";

setupDom();
const t = (key: string) => `[${key}]`;

const rows = (host: HTMLElement) =>
  Object.fromEntries(
    Array.from(host.querySelectorAll("[data-row]")).map((node) => [
      node.getAttribute("data-row"),
      node.querySelector("dd")?.textContent ?? "",
    ]),
  );

describe("the requirements card", () => {
  test("shows every requirement of a reset-policy session", async () => {
    const { host } = await render(
      <RequirementsCard requirements={requirements()} />,
    );
    const got = rows(host);
    expect(got.instruction).toBe("stack the plates");
    expect(got.reset_instruction).toBe("unstack the plates");
    expect(got.episodes).toBe("12");
    expect(got.forward_steps).toBe("300");
    expect(got.reset_steps).toBe("200");
    expect(got.early_stop).toBe("Allowed");
    expect(got.forward_policy).toBe("fwd-a · pi05_fr3");
    expect(got.reset_policy).toBe("rst-a · pi05_reset");
    expect(got.inference).toContain("Pause on an event and judge");
    expect(got.inference).toContain("physical hold");
    expect(got.success_condition).toContain(
      "All plates of one colour are stacked.",
    );
  });

  test("says the success condition is only recorded and shown", async () => {
    const { host } = await render(
      <RequirementsCard requirements={requirements()} />,
    );
    const text = host.querySelector(
      "[data-row=success_condition]",
    )!.textContent!;
    expect(text).toContain(
      "Recorded and shown only; it takes no part in any verdict.",
    );
    expect(text.toLowerCase()).not.toContain("judging spec");
    expect(text.toLowerCase()).not.toContain("is judged by");
  });

  test("a manual-reset session has no reset rows", async () => {
    const { host } = await render(
      <RequirementsCard
        requirements={requirements({ reset_mode: "human_assisted" })}
      />,
    );
    const got = rows(host);
    expect(got.reset_instruction).toBeUndefined();
    expect(got.reset_steps).toBeUndefined();
    expect(got.reset_policy).toBeUndefined();
    expect(got.forward_policy).toBeDefined();
  });

  test("a value the server did not send is left out, not invented", async () => {
    const { host } = await render(
      <RequirementsCard
        requirements={{ task: { instruction: "go" }, episodes: 3 }}
      />,
    );
    expect(Object.keys(rows(host))).toEqual(["instruction", "episodes"]);
  });

  test("an old server (nothing, or an empty object) shows no card", async () => {
    expect(hasRequirements(undefined)).toBe(false);
    expect(hasRequirements(null)).toBe(false);
    expect(hasRequirements({})).toBe(false);
    for (const value of [undefined, null, {}]) {
      const { host } = await render(<RequirementsCard requirements={value} />);
      expect(host.querySelector("[data-testid=requirements]")).toBeNull();
    }
  });

  test("a job that names no checkpoint says so", async () => {
    const { host } = await render(
      <RequirementsCard
        requirements={requirements({
          policies: { forward: null, reset: null },
        })}
      />,
    );
    expect(rows(host).forward_policy).toBe("Not recorded in the job file");
  });
});

describe("step progress", () => {
  test("a number is a lower bound and says why", () => {
    expect(
      stepProgressText({ step: 42, max_steps: 300, lower_bound: true }, t),
    ).toBe("≥ 42 / 300 ([automatic.req.step_lower_bound])");
  });
  test("an exact number carries no bound", () => {
    expect(
      stepProgressText({ step: 42, max_steps: 300, lower_bound: false }, t),
    ).toBe("42 / 300");
  });
  test("an unknown step is a dash with the reason, never a number", () => {
    expect(
      stepProgressText(
        { step: null, max_steps: 300, reason: "runner_not_alive" },
        t,
      ),
    ).toBe("— / 300 ([automatic.req.step_reason.runner_not_alive])");
    expect(stepProgressText({ step: null }, t)).toBe("—");
  });
});
