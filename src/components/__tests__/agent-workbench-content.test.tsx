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

describe("ds controls inside the drawer win over levi.css's dock rules", () => {
  test("every ds.css rule of a button-like control is restated under the dock", () => {
    const strip = (css: string) => css.replace(/\/\*[\s\S]*?\*\//g, "");
    const ds = strip(
      readFileSync(join(import.meta.dir, "../../styles/ds.css"), "utf8"),
    );
    const agent = strip(read("pages-ui/agent-content.css")).replace(
      /\s+/g,
      " ",
    );
    const control =
      /^\.ds-(btn|icon-btn|menu__item|menu-trigger|tab|tag__remove|reorder__handle|toast__action)\b|^\.ds-focus:focus-visible/;
    const missing: string[] = [];
    for (const m of ds.matchAll(/([^{}@]+)\{[^{}]*\}/g))
      for (const sel of m[1].split(",").map((s) => s.trim()))
        if (control.test(sel) && !agent.includes(`.levi-agent-dock ${sel}`))
          missing.push(sel);
    expect(missing).toEqual([]);
    for (const variant of ["primary", "secondary", "ghost", "danger"])
      expect(agent).toContain(`.levi-agent-dock .ds-btn--${variant} {`);
    expect(agent).toContain(".levi-agent-dock .ds-btn--ghost:hover");
    expect(agent).toContain(".levi-agent-dock .ds-btn:disabled");
    expect(agent).toContain(".levi-agent-dock .ds-focus:focus-visible");
  });
});
