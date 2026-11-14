import {
  flush,
  focus,
  press,
  render,
  setupDom,
} from "@/components/ds/__tests__/dom";
import { describe, expect, mock, test } from "bun:test";

const pushed: string[] = [];
mock.module("next/navigation", () => ({
  useRouter: () => ({ push: (href: string) => pushed.push(href) }),
  usePathname: () => "/pool",
}));
const { ShellProvider } = await import("../shell-context");
const { CHORD_PAGES, CHORD_WINDOW_MS, chordPage, shortcutGroups } =
  await import("../global-keys");

setupDom();

const chip = () => document.querySelector(".levi-chord-chip");
async function frame() {
  pushed.length = 0;
  await render(
    <ShellProvider navigate={(href) => pushed.push(href)}>
      <button type="button" id="before">
        Before
      </button>
      <input id="field" />
    </ShellProvider>,
  );
  const before = document.querySelector<HTMLButtonElement>("#before")!;
  await focus(before);
  return before;
}

describe("the G chord chip", () => {
  test("shows after G, and goes when the letter comes", async () => {
    const before = await frame();
    expect(chip()).toBeNull();
    await press(before, "g");
    expect(chip()?.getAttribute("role")).toBe("status");
    expect(chip()?.textContent).toContain("G");
    await press(before, "a");
    expect(pushed).toEqual(["/automatic"]);
    expect(chip()).toBeNull();
  });

  test("goes at any other key (a wrong letter, Escape) and when typing in a field", async () => {
    const before = await frame();
    await press(before, "g");
    await press(before, "x");
    expect(chip()).toBeNull();
    await press(before, "g");
    await press(before, "Escape");
    expect(chip()).toBeNull();
    // G typed into a field is text, not a chord
    const field = document.querySelector<HTMLInputElement>("#field")!;
    await focus(field);
    await press(field, "g");
    expect(chip()).toBeNull();
    expect(pushed).toEqual([]);
  });

  test("goes by itself when the chord window ends", async () => {
    const before = await frame();
    await press(before, "g");
    expect(chip()).not.toBeNull();
    await flush(CHORD_WINDOW_MS + 150);
    expect(chip()).toBeNull();
  });
});

describe("chords for Automatic evaluation and Robot Studio", () => {
  test("G A and G S, with letters that are unique", () => {
    expect(chordPage({ key: "a" })).toBe("/automatic");
    expect(chordPage({ key: "s" })).toBe("/robot-studio");
    expect(Object.keys(CHORD_PAGES).filter((k) => k === "g")).toEqual([]);
    const hrefs = Object.values(CHORD_PAGES).map((p) => p.href);
    expect(new Set(hrefs).size).toBe(hrefs.length);
  });

  test("the shortcut list names both chords and the Robot Studio Esc", () => {
    const groups = shortcutGroups(false);
    const everywhere = groups.find((g) => g.title === "Everywhere")!.rows;
    for (const letter of ["A", "S"])
      expect(everywhere.some((r) => r.keys[0].join("") === `G${letter}`)).toBe(
        true,
      );
    const studio = groups.find((g) => g.title === "Robot Studio")!;
    expect(studio.rows[0].keys).toEqual([["Esc"]]);
    expect(studio.rows[0].label).toMatch(/manual translation session/);
  });
});
