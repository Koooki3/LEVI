import { describe, expect, test } from "bun:test";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";

/**
 * The page has two real-robot controls (Real gripper control, Manual relative
 * translation), so no wording may say the page sends no robot commands. The
 * text of the scope note is what a person reads before touching either.
 */
const KEYS = [
  "robotStudio.scopeLine",
  "robotStudio.scope",
  "robotStudio.next",
  "robotStudio.state.workspaceDescription",
  "robotStudio.state.readOnlyMapping",
] as const;
type Catalog = Record<string, string>;

describe("Robot Studio scope wording", () => {
  for (const [name, catalog] of [
    ["en", en as Catalog],
    ["zh", zh as Catalog],
  ] as const) {
    test(`${name}: no key claims the page sends no robot commands`, () => {
      for (const key of KEYS) {
        const text = catalog[key] ?? "";
        // "send no", "sends no", "never sent to the robot", "send nothing":
        // the 3D target IS sent in a manual translation session.
        expect(text).not.toMatch(
          /sends? no (robot )?commands|never sent to the robot|send nothing|不会(驱动机器人|发送任何指令)|不发送机器人指令/i,
        );
        expect(text).not.toMatch(/not connected to a robot command interface/);
        expect(text).not.toMatch(/no motion|不执行运动/);
      }
    });
  }
  test("the scope lines name both real controls", () => {
    for (const key of ["robotStudio.scopeLine", "robotStudio.scope"]) {
      expect(en[key as keyof typeof en]).toMatch(/gripper/i);
      expect(en[key as keyof typeof en]).toMatch(/translation/i);
      expect(zh[key as keyof typeof zh]).toContain("夹爪");
      expect(zh[key as keyof typeof zh]).toContain("平移");
    }
  });

  test("the wording says that dragging the 3D target moves the real arm in a started session", () => {
    // Dragging the target feeds the manual translation session
    // (studio.tsx onTarget -> manualController.target); a text that calls the
    // target offline without this would be read as "safe to drag".
    for (const key of [
      "robotStudio.scope",
      "robotStudio.scopeLine",
      "robotStudio.state.workspaceDescription",
      "robotStudio.next",
    ] as const) {
      expect([key, /drag/i.test(en[key])]).toEqual([key, true]);
      expect([key, /拖动/.test(zh[key])]).toEqual([key, true]);
      expect([key, /session|会话/i.test(en[key] + zh[key])]).toEqual([
        key,
        true,
      ]);
    }
  });
});
