import { click, render, setupDom } from "./dom";
import { describe, expect, test } from "bun:test";
import { ExpandableText } from "../ExpandableText";
setupDom();
describe("complete long text", () => {
  test("short text remains inline", async () => {
    const { host } = await render(<ExpandableText text="short path" mono />);
    expect(host.querySelector("details")).toBeNull();
    expect(host.querySelector("code")?.textContent).toBe("short path");
  });
  test("long unicode text has a selectable full body and copies exactly", async () => {
    const text = "夹取物体/" + "a".repeat(260) + "\n保持完整";
    const { host } = await render(<ExpandableText text={text} />);
    expect(host.querySelector(".ds-long-text__body")?.textContent).toBe(text);
    expect(host.querySelector("summary")?.textContent).toContain("Full text");
    let copied = "";
    const original = navigator.clipboard.writeText;
    navigator.clipboard.writeText = async (value: string) => {
      copied = value;
    };
    try {
      await click(host.querySelector("button"));
      expect(copied).toBe(text);
    } finally {
      navigator.clipboard.writeText = original;
    }
  });
});
