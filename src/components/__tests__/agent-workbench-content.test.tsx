import { render, setupDom } from "../ds/__tests__/dom";
import { describe, expect, test } from "bun:test";
import { readFileSync } from "fs";
import { join } from "path";
import { HumanActionMark } from "../pages-ui/feedback";

setupDom();

const read = (file: string) =>
  readFileSync(join(import.meta.dir, "..", file), "utf8");
const strip = (css: string) => css.replace(/\/\*[\s\S]*?\*\//g, "");

/** The files that make the Agent Workbench's four sections. */
const CONTENT = [
  "agent-workbench.tsx",
  "agent-activity.tsx",
  "agent-connections.tsx",
  "agent-definitions.tsx",
  "agent-object-tool.tsx",
  "agent-pilot.tsx",
  "agent-plan.tsx",
  "agent-quality.tsx",
  "agent-review-queue.tsx",
  "agent-runtime-connections.tsx",
  "agent-supervision.tsx",
  "agent-task-console.tsx",
  "chip-multi-select.tsx",
  "ollama-models.tsx",
  "ollama-runtime.tsx",
  "hf-auth-button.tsx",
];

/** The opening tag of the Button (or GatedButton) that carries `label`. */
const buttonOf = (code: string, label: string) => {
  const at = code.indexOf(label);
  expect(at).toBeGreaterThan(0);
  const open = Math.max(
    code.lastIndexOf("<Button", at),
    code.lastIndexOf("<GatedButton", at),
  );
  return code.slice(open, at);
};

describe("Agent Workbench content", () => {
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
    const plan = read("agent-plan.tsx");
    expect(plan.match(/<HumanActionMark \/>/g)?.length).toBe(2);
    expect(buttonOf(plan, "Approve execution plan")).toContain(
      'variant="primary"',
    );
    expect(buttonOf(plan, "Accept pilot quality")).toContain(
      'variant="primary"',
    );
    const workbench = read("agent-workbench.tsx");
    expect(buttonOf(workbench, "Commit approved changes")).toContain(
      'variant="primary"',
    );
    expect(buttonOf(workbench, "Cancel task")).toContain('variant="ghost"');
    // Validate & approve, commit, the task approval and a runtime permission
    // are the other actions only a person takes.
    expect(workbench.match(/<HumanActionMark \/>/g)?.length).toBe(2);
    expect(read("agent-task-console.tsx")).toContain("<HumanActionMark />");
    expect(read("agent-pilot.tsx")).toContain("<HumanActionMark />");
  });

  test("no raw button, select, textarea or input is hand-written in the content", () => {
    for (const file of CONTENT) {
      const code = read(file);
      const raw = code.match(/<(button|select|textarea|input)\b/g);
      expect([file, raw]).toEqual([file, null]);
    }
  });

  test("no hand-drawn icon, emoji arrow or native title on a button", () => {
    for (const file of CONTENT) {
      const code = read(file)
        .replace(/\/\*[\s\S]*?\*\//g, "")
        .replace(/^\s*\/\/.*$/gm, "");
      expect([file, code.match(/<svg\b|react-icons/g)]).toEqual([file, null]);
      // A native title="" tooltip on a plain element (ds components take
      // `title` as a prop of their own, and a Button may keep one for a label
      // that is cut short).
      expect([
        file,
        code.match(/<(?:button|span|div|a|p|summary)\b[^>]*\stitle=/g),
      ]).toEqual([file, null]);
    }
  });

  test("the content uses only classes some live stylesheet defines (levi.css is gone)", () => {
    // levi-agent-sheet and levi-agent-grip belong to the frame; the levi-hf
    // and levi-format classes are defined in shared.css, next to the code.
    const allowed = /^levi-(agent-(sheet|grip)|hf-|format)/;
    for (const file of [
      ...CONTENT,
      "dataset-format.tsx",
      "agent-ui.tsx",
      "draggable-popup.tsx",
    ]) {
      const code = read(file);
      const used = [
        ...code.matchAll(/className=(?:"([^"]*)"|\{`([^`]*)`\})/g),
      ].flatMap((m) => (m[1] ?? m[2]).split(/\s+/));
      const old = used.filter((c) => c.startsWith("levi-") && !allowed.test(c));
      expect([file, old]).toEqual([file, []]);
    }
  });

  test("the HF sign-in is a ds Button and loads nothing from huggingface.co", () => {
    const code = read("hf-auth-button.tsx");
    expect(code).not.toContain("huggingface.co/datasets");
    expect(code).not.toMatch(/<img\b/);
    expect(code).toContain("LogIn");
  });
});

describe("agent-content.css", () => {
  const css = strip(read("pages-ui/agent-content.css"));

  test("lays out ds components; it does not restate their look", () => {
    expect(css).not.toContain("levi-agent-dock");
    for (const selector of [
      ".ds-btn {",
      ".ds-btn--primary",
      ".ds-btn--secondary",
      ".ds-focus:focus-visible",
      ".ds-tab {",
      ".ds-icon-btn {",
    ])
      expect(css).not.toMatch(
        new RegExp(`(^|\\n)${selector.replace(/[.{}]/g, "\\$&")}`),
      );
  });

  test("tokens only: no colour literal, no infinite animation, weights 400-600", () => {
    expect(css.match(/#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(/g)).toBeNull();
    expect(css).not.toMatch(/infinite|@keyframes|animation:\s*(?!\s|none)/);
    expect(css.match(/font-weight:\s*[^;]+/g)?.sort()).toEqual(
      css
        .match(/font-weight:\s*[^;]+/g)
        ?.filter((w) => /var\(--ds-weight-(regular|medium|semibold)\)/.test(w))
        .sort(),
    );
    // No size below 12px: sizes come from the type tokens or are not set.
    const sizes = css.match(/font-size:[^;]+/g) ?? [];
    expect(
      sizes.filter((v) => !/var\(--ds-text-[a-z0-9-]+-size\)/.test(v)),
    ).toEqual([]);
  });

  test("motion goes through the duration tokens, which reduced motion zeroes", () => {
    const transitions = (css.match(/transition:[^;]+;/g) ?? []).filter(
      (rule) => !/transition:\s*none/.test(rule),
    );
    expect(transitions.length).toBeGreaterThan(0);
    for (const rule of transitions)
      expect(rule).toMatch(/var\(--ds-dur-(base|slow)\)/);
  });
});
