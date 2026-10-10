import { click, render, setupDom } from "@/components/ds/__tests__/dom";
import { describe, expect, test } from "bun:test";
import { SetupGuide, SetupGuideLoader } from "../setup-guide";
import {
  copyAllowed,
  copyBlockedKey,
  effectiveMode,
  needsSafetyChecklist,
  readSteps,
} from "../setup-logic";
import type { SetupStep } from "../wizard-types";

setupDom();

const step = (over: Partial<SetupStep>): SetupStep => ({
  id: "R-X",
  title: { en: "A step", zh: "一步" },
  level: 1,
  mode: "copy",
  command: "echo hi",
  why: { en: "Because", zh: "因为" },
  status: "todo",
  ...over,
});

describe("setup rules", () => {
  test("only a level 4 step that is not native needs the safety list", () => {
    expect(needsSafetyChecklist(step({ level: 4 }))).toBe(true);
    expect(needsSafetyChecklist(step({ level: 3 }))).toBe(false);
    expect(needsSafetyChecklist(step({ level: 4, mode: "native" }))).toBe(
      false,
    );
  });

  test("a level 4 command is not offered until the list is ticked", () => {
    const s = step({ level: 4 });
    expect(copyAllowed(s, false)).toBe(false);
    expect(copyBlockedKey(s, false)).toBe("automatic.setup.blocked.safety");
    expect(copyAllowed(s, true)).toBe(true);
    expect(copyBlockedKey(s, true)).toBeNull();
  });

  test("a native step is never copied and an execute step without an executor is a copy step", () => {
    expect(copyAllowed(step({ mode: "native" }), true)).toBe(false);
    expect(effectiveMode(step({ mode: "execute" }), false)).toBe("copy");
    expect(effectiveMode(step({ mode: "execute" }), true)).toBe("execute");
  });

  test("a step the page cannot understand is dropped, not shown wrongly", () => {
    const steps = readSteps({
      steps: [step({}), { id: "bad", title: {}, level: 9 }, null],
    });
    expect(steps.map((s) => s.id)).toEqual(["R-X"]);
  });
});

describe("the setup guide", () => {
  test("shows copy, native and execute steps for what they are", async () => {
    const { host } = await render(
      <SetupGuide
        steps={[
          step({ id: "c", mode: "copy", command: "echo copy-me" }),
          step({ id: "n", mode: "native", command: undefined, status: "ok" }),
          step({ id: "e", mode: "execute", command: "echo run-me" }),
        ]}
      />,
    );
    const text = host.textContent ?? "";
    expect(text).toContain("echo copy-me");
    expect(text).toContain("LEVI reads this status itself");
    // No executor wired for the execute step: it is a command to copy, and
    // there is no Run button.
    expect(text).toContain("echo run-me");
    expect(text).toContain("LEVI cannot run this step from here");
    expect(text).not.toContain("Run this step");
    expect(text).toContain("emergency stop");
  });

  test("an execute step with an executor gets a Run button that calls it", async () => {
    let calls = 0;
    const { host } = await render(
      <SetupGuide
        steps={[step({ id: "e", mode: "execute", command: "x" })]}
        executors={{
          e: async () => {
            calls += 1;
          },
        }}
      />,
    );
    const run = Array.from(host.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Run this step"),
    );
    expect(run).toBeTruthy();
    await click(run!);
    expect(calls).toBe(1);
  });

  test("a level 4 command stays hidden until all four safety items are ticked", async () => {
    const { host } = await render(
      <SetupGuide
        steps={[step({ id: "m", level: 4, command: "move-the-arm" })]}
      />,
    );
    expect(host.textContent).not.toContain("move-the-arm");
    expect(host.textContent).toContain("Tick the safety list");
    expect(host.textContent).toContain("are NOT emergency stops");
    const boxes = Array.from(
      host.querySelectorAll<HTMLInputElement>("fieldset input[type=checkbox]"),
    );
    expect(boxes.length).toBe(4);
    for (const box of boxes.slice(0, 3)) await click(box);
    expect(host.textContent).not.toContain("move-the-arm");
    await click(boxes[3]);
    expect(host.textContent).toContain("move-the-arm");
  });

  test("the loader shows a problem with Try again when the guide cannot be read", async () => {
    let attempts = 0;
    const { host } = await render(
      <SetupGuideLoader
        load={async () => {
          attempts += 1;
          if (attempts === 1) throw new Error("HTTP 502");
          return { steps: [step({ id: "ok1", status: "ok" })] };
        }}
      />,
    );
    await new Promise((r) => setTimeout(r, 20));
    expect(host.textContent).toContain("HTTP 502");
    const retry = Array.from(host.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Try again"),
    );
    await click(retry!);
    await new Promise((r) => setTimeout(r, 20));
    expect(host.textContent).toContain("A step");
    expect(attempts).toBe(2);
  });
});

describe("the wording of a step (P6)", () => {
  test("a step with no command does not tell the person to copy one", async () => {
    const { host } = await render(
      <SetupGuide steps={[step({ command: undefined })]} />,
    );
    const text = host.textContent ?? "";
    expect(text).toContain("No command is given for this step.");
    expect(text).not.toContain("Copy the command");
  });
  test("a step with a command still says to copy it", async () => {
    const { host } = await render(<SetupGuide steps={[step({})]} />);
    expect(host.textContent).toContain("Copy the command and run it yourself");
  });
  test("a status LEVI reads itself does not claim there is nothing to do", async () => {
    const { host } = await render(
      <SetupGuide
        steps={[step({ mode: "native", command: undefined, status: "warn" })]}
      />,
    );
    expect(host.textContent).toContain("there is no command for it");
    expect(host.textContent).not.toContain("nothing to run");
  });
});
