import { render, setupDom } from "@/components/ds/__tests__/dom";
import { describe, expect, test } from "bun:test";
import type { Pulse, PulseLight } from "@/components/live/live-logic";

const { PulseDot, PULSE_WORD } = await import("@/components/live/live-nav");

setupDom();

const LIGHTS: PulseLight[] = ["green", "blue", "amber", "red", "grey"];

async function draw(light: PulseLight) {
  const pulse = { light, reason: "idle", count: 0 } as unknown as Pulse;
  const { host } = await render(<PulseDot pulse={pulse} />);
  return host;
}

describe("live state dot", () => {
  test("each state has its own shape, not only its own colour", async () => {
    const shapes = new Set<string>();
    for (const light of LIGHTS) {
      const host = await draw(light);
      const svg = host.querySelector("svg");
      expect(svg).not.toBeNull();
      shapes.add(
        [...(svg?.classList ?? [])].find((name) =>
          name.startsWith("lucide-"),
        ) ?? "",
      );
    }
    expect(shapes.size).toBe(LIGHTS.length);
  });

  test("the state is in words for screen readers", async () => {
    for (const light of LIGHTS) {
      const host = await draw(light);
      const words = host.querySelector(".ds-sr-only");
      expect(words?.textContent).toBe(PULSE_WORD[light]);
    }
  });

  test("only labelling breathes", async () => {
    for (const light of LIGHTS) {
      const host = await draw(light);
      expect(Boolean(host.querySelector(".ds-breathe"))).toBe(light === "blue");
    }
  });
});
