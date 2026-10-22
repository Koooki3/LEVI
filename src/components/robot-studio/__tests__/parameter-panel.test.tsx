import { click, render, setupDom } from "@/components/ds/__tests__/dom";
import { act } from "react";
import { describe, expect, test } from "bun:test";
import ParameterPanel from "../parameter-panel";

setupDom();
const button = (host: HTMLElement, name: string) =>
  Array.from(host.querySelectorAll("button")).find(
    (node) => node.textContent?.trim() === name,
  )!;
async function type(input: HTMLInputElement, value: string) {
  await act(async () => {
    input.focus();
    Object.getOwnPropertyDescriptor(
      HTMLInputElement.prototype,
      "value",
    )!.set!.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(
      new KeyboardEvent("keyup", { key: "e", bubbles: true }),
    );
  });
}

describe("parameter capability inspector", () => {
  test("mount does not probe a device and exposes unknown values without an apply action", async () => {
    const original = globalThis.fetch;
    const calls: unknown[] = [];
    globalThis.fetch = ((...args: unknown[]) => {
      calls.push(args);
      throw new Error("parameter panel must be local");
    }) as typeof fetch;
    try {
      const { host } = await render(<ParameterPanel />);
      expect(host.textContent).toContain("Controller parameter readback");
      expect(host.textContent).toContain("Unavailable");
      for (const mode of [
        "preview_only",
        "acknowledgement_pending",
        "restart_required",
        "unsupported",
      ]) {
        expect(host.querySelector(`[data-mode="${mode}"]`)).not.toBeNull();
      }
      expect(host.querySelectorAll("button")).toHaveLength(2);
      expect(button(host, "Export proposal").disabled).toBe(true);
      expect(
        Array.from(host.querySelectorAll("input")).every(
          (input) => input.value === "",
        ),
      ).toBe(true);
      expect(calls).toEqual([]);
    } finally {
      globalThis.fetch = original;
    }
  });

  test("partial joint input blocks export with an associated error; clear restores unknown", async () => {
    const exported: string[] = [];
    const { host } = await render(
      <ParameterPanel
        onExport={(value) => {
          exported.push(value);
        }}
      />,
    );
    const input = host.querySelector<HTMLInputElement>(
      'input[aria-label="Joint torque limit proposal J1 (N·m)"]',
    )!;
    await type(input, "1");
    expect(input.getAttribute("aria-invalid")).toBe("true");
    expect(
      host.querySelector("#" + input.getAttribute("aria-describedby"))
        ?.textContent,
    ).toContain("Complete this group");
    expect(button(host, "Export proposal").disabled).toBe(true);
    await click(button(host, "Export proposal"));
    expect(exported).toEqual([]);
    await click(button(host, "Clear proposal"));
    expect(input.value).toBe("");
    expect(input.getAttribute("aria-invalid")).toBe("false");
    expect(button(host, "Clear proposal").disabled).toBe(true);
  });

  test("exporting a scalar proposal preserves every unknown group and grants no online capability", async () => {
    const exported: string[] = [];
    const { host } = await render(
      <ParameterPanel
        onExport={(value) => {
          exported.push(value);
        }}
      />,
    );
    const input = host.querySelector<HTMLInputElement>(
      'input[aria-label="External-force stop threshold proposal (N)"]',
    )!;
    await type(input, "2");
    expect(button(host, "Export proposal").disabled).toBe(false);
    await click(button(host, "Export proposal"));
    expect(exported).toHaveLength(1);
    const doc = JSON.parse(exported[0]);
    expect(doc.proposed_limits.external_force_stop_threshold_n).toEqual([2]);
    expect(doc.proposed_limits.joint_torque_limit_nm).toBeNull();
    expect(
      doc.capabilities.every(
        (entry: { can_apply: boolean }) => !entry.can_apply,
      ),
    ).toBe(true);
    expect(host.textContent).toContain("Unavailable");
  });
});
