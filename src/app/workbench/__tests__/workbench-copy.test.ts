import { describe, expect, test } from "bun:test";
import zh from "@/i18n/zh.json";

/**
 * The glossary: 指令 is a robot or task instruction, 命令 is a shell command.
 * The conversion preview shows the shell command that would run.
 */
describe("workbench Chinese wording", () => {
  test("the command preview error says 命令, not 指令", () => {
    const text = (zh as Record<string, string>)[
      "The command could not be previewed"
    ];
    expect(text).toBe("无法预览命令");
    expect(text).not.toContain("指令");
  });
});
