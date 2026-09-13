import { describe, expect, test } from "bun:test";
import {
  extractLanguageInstructions,
  extractTaskFromMetadata,
} from "@/utils/languageInstructions";

describe("language metadata helpers", () => {
  test("finds numbered instructions even when the primary field is absent", () => {
    expect(
      extractLanguageInstructions([
        { language_instruction_2: "second", language_instruction_3: "third" },
      ]),
    ).toBe("second\nthird");
  });

  test("matches task_index rather than physical task table order", () => {
    expect(
      extractTaskFromMetadata("2", [
        { task_index: 1, task: "one" },
        { task_index: 2, task: "two" },
      ]),
    ).toBe("two");
  });
});
