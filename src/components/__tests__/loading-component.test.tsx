import { render, setupDom } from "@/components/ds/__tests__/dom";
import { describe, expect, test } from "bun:test";
import Loading from "../loading-component";

setupDom();

describe("loading overlay", () => {
  test("is a busy status region without focus or a dialog role", async () => {
    const { host } = await render(<Loading />);
    const region = host.querySelector('[role="status"]')!;
    expect(region).not.toBeNull();
    expect(region.getAttribute("aria-busy")).toBe("true");
    expect(region.getAttribute("tabindex")).toBeNull();
    expect(region.getAttribute("aria-modal")).toBeNull();
    expect(host.querySelector('[role="dialog"]')).toBeNull();
    // The spinner is decoration.
    expect(host.querySelector("svg")!.getAttribute("aria-hidden")).toBe("true");
  });
});
