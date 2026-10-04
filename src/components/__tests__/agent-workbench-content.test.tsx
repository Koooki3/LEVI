import { render, setupDom } from "../ds/__tests__/dom";
import { describe, expect, test } from "bun:test";
import { readFileSync } from "fs";
import { join } from "path";
import { HumanActionMark } from "../pages-ui/feedback";

setupDom();

const read = (file: string) =>
  readFileSync(join(import.meta.dir, "..", file), "utf8");

describe("Agent Workbench content (stage 4)", () => {
  test("a person-only action is marked with words and an icon", async () => {
    const { host } = await render(<HumanActionMark />);
    const badge = host.querySelector(".ds-badge")!;
    expect(badge.textContent).toBe("Needs your confirmation");
    expect(badge.querySelector("svg")).not.toBeNull();
  });

  test("the drawer's sections are ds Tabs, not pressed buttons", () => {
    const code = read("agent-workbench.tsx");
    expect(code).toContain("<Tabs");
    expect(code).not.toMatch(/aria-pressed=\{tab ===/);
  });

  test("approve, accept-pilot and commit carry the mark and are the primary", () => {
    const buttonOf = (code: string, label: string) => {
      const at = code.indexOf(label);
      return code.slice(code.lastIndexOf("<button", at), at);
    };
    const plan = read("agent-plan.tsx");
    expect(plan.match(/<HumanActionMark \/>/g)?.length).toBe(2);
    expect(buttonOf(plan, "Approve execution plan")).toContain(
      "ds-btn--primary",
    );
    expect(buttonOf(plan, "Accept pilot quality")).toContain("ds-btn--primary");
    const workbench = read("agent-workbench.tsx");
    expect(workbench).toContain("<HumanActionMark />");
    expect(buttonOf(workbench, "Commit approved changes")).toContain(
      "ds-btn--primary",
    );
    expect(buttonOf(workbench, "Cancel task")).toContain("ds-btn--ghost");
  });

  test("every button and field of the drawer content has a ds class", () => {
    for (const file of [
      "agent-workbench.tsx",
      "agent-plan.tsx",
      "agent-pilot.tsx",
      "agent-review-queue.tsx",
      "agent-connections.tsx",
    ]) {
      const code = read(file);
      const bare = code.match(
        /<(button|select|textarea)(?![^>]*className)\s*\n?\s*[a-z]/g,
      );
      expect([file, bare]).toEqual([file, null]);
    }
  });
});
