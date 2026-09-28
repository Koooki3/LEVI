import { describe, expect, test } from "bun:test";
import { urlSeekTarget } from "@/utils/urlTime";

describe("urlSeekTarget", () => {
  test("seeks to a ?t= the page did not write itself", () => {
    expect(urlSeekTarget("12.5", null)).toBe(12.5);
    expect(urlSeekTarget("26", "25")).toBe(26);
  });

  test("ignores the whole second the page just mirrored into the URL", () => {
    // A click at 26.9 s is mirrored as t=26; seeking to it would undo it.
    expect(urlSeekTarget("26", "26")).toBeNull();
  });

  test("ignores a missing or unparsable value", () => {
    expect(urlSeekTarget(null, null)).toBeNull();
    expect(urlSeekTarget("", null)).toBeNull();
    expect(urlSeekTarget("abc", null)).toBeNull();
  });
});
