import { describe, expect, test } from "bun:test";
import { isSaveShortcut } from "@/utils/keyboardShortcuts";

describe("isSaveShortcut", () => {
  test("accepts Ctrl+S and Cmd+S", () => {
    expect(
      isSaveShortcut({
        ctrlKey: true,
        metaKey: false,
        altKey: false,
        key: "s",
      }),
    ).toBe(true);
    expect(
      isSaveShortcut({
        ctrlKey: false,
        metaKey: true,
        altKey: false,
        key: "S",
      }),
    ).toBe(true);
  });

  test("does not treat Alt+S, IME composition or an unrelated key as Save", () => {
    expect(
      isSaveShortcut({ ctrlKey: true, metaKey: false, altKey: true, key: "s" }),
    ).toBe(false);
    expect(
      isSaveShortcut({
        ctrlKey: true,
        metaKey: false,
        altKey: false,
        isComposing: true,
        key: "s",
      }),
    ).toBe(false);
    expect(
      isSaveShortcut({
        ctrlKey: true,
        metaKey: false,
        altKey: false,
        key: "z",
      }),
    ).toBe(false);
  });
});
