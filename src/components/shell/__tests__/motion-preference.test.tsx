import { act, type ReactNode } from "react";
import {
  flush,
  mockMatchMedia,
  render,
  setupDom,
} from "@/components/ds/__tests__/dom";
import { Progress } from "@/components/ds";
import { describe, expect, test } from "bun:test";
import { readFileSync } from "fs";
import { join } from "path";
import {
  usePrefersReducedMotion,
  prefersReducedMotion,
} from "@/lib/design/motion";
import {
  MOTION_DEFAULT_PREFERENCE,
  MOTION_STORAGE_KEY,
  applyMotion,
  normalizeMotionPreference,
} from "../motion-preference";
import { MOTION_BOOT_KEY, THEME_BOOT_SCRIPT } from "../theme-boot";

setupDom();

function Probe(): ReactNode {
  return <span id="probe">{String(usePrefersReducedMotion())}</span>;
}

describe("the motion setting", () => {
  test("follows the system unless a choice is stored", () => {
    expect(MOTION_DEFAULT_PREFERENCE).toBe("system");
    expect(normalizeMotionPreference(null)).toBe("system");
    expect(normalizeMotionPreference("nonsense")).toBe("system");
    expect(normalizeMotionPreference("reduce")).toBe("reduce");
    expect(normalizeMotionPreference("full")).toBe("full");
  });

  test("applies to <html> as data-motion; system removes it", () => {
    const el = document.createElement("html");
    applyMotion(el, "reduce");
    expect(el.getAttribute("data-motion")).toBe("reduce");
    applyMotion(el, "full");
    expect(el.getAttribute("data-motion")).toBe("full");
    applyMotion(el, "system");
    expect(el.hasAttribute("data-motion")).toBe(false);
  });

  test("the first-paint script applies a stored choice and ignores junk", () => {
    expect(MOTION_BOOT_KEY).toBe(MOTION_STORAGE_KEY);
    const run = (value: string | null) => {
      const attributes: Record<string, string> = {};
      new Function("document", "localStorage", THEME_BOOT_SCRIPT)(
        {
          documentElement: {
            setAttribute: (n: string, v: string) => {
              attributes[n] = v;
            },
          },
        },
        {
          getItem: (key: string) => (key === MOTION_STORAGE_KEY ? value : null),
        },
      );
      return attributes["data-motion"] ?? null;
    };
    expect(run("reduce")).toBe("reduce");
    expect(run("full")).toBe("full");
    expect(run("system")).toBeNull();
    expect(run(null)).toBeNull();
    expect(run("dark")).toBeNull();
  });

  test("tokens: 'normal' keeps motion under a system request for less", () => {
    const css = readFileSync(
      join(import.meta.dir, "../../../styles/tokens.css"),
      "utf8",
    );
    expect(css).toMatch(
      /prefers-reduced-motion: reduce\)\s*\{[^{]*:root:not\(\[data-motion="full"\]\)/,
    );
    expect(css).toMatch(/\[data-motion="reduce"\]\s*\{/);
  });

  test("components that render differently follow the setting live", async () => {
    document.documentElement.removeAttribute("data-motion");
    const { host } = await render(<Probe />);
    const value = () => host.querySelector("#probe")!.textContent;
    expect(value()).toBe("false");
    await act(async () => {
      document.documentElement.setAttribute("data-motion", "reduce");
    });
    await flush();
    expect(value()).toBe("true");
    expect(prefersReducedMotion()).toBe(true);
    await act(async () => {
      document.documentElement.setAttribute("data-motion", "full");
    });
    await flush();
    expect(value()).toBe("false");
    expect(prefersReducedMotion()).toBe(false);
    document.documentElement.removeAttribute("data-motion");
  });

  test("the settings menu offers the three choices", () => {
    const header = readFileSync(
      join(import.meta.dir, "../../levi-header.tsx"),
      "utf8",
    );
    for (const key of [
      "Motion: follow the system",
      "Motion: reduced",
      "Motion: normal",
    ])
      expect(header).toContain(key);
  });

  test("'normal' keeps the indeterminate bar moving when the system asks for less", async () => {
    const restore = mockMatchMedia(["prefers-reduced-motion"]);
    try {
      document.documentElement.removeAttribute("data-motion");
      const { host } = await render(<Progress label="Reading" value={null} />);
      const track = () => host.querySelector(".ds-progress__track")!;
      await flush();
      // The system asks for less: still, with the words.
      expect(track().className).toContain("ds-progress__track--still");
      await act(async () => {
        document.documentElement.setAttribute("data-motion", "full");
      });
      await flush();
      // The person chose normal: moving, not a frozen 30 %.
      expect(track().className).not.toContain("ds-progress__track--still");
      expect(host.textContent).not.toContain("In progress");
    } finally {
      document.documentElement.removeAttribute("data-motion");
      restore();
    }
  });

  test("the stylesheet's media query stands down under 'normal'", () => {
    const css = readFileSync(
      join(import.meta.dir, "../../../styles/ds.css"),
      "utf8",
    );
    expect(css).toMatch(
      /@media \(prefers-reduced-motion: reduce\) \{[^}]*:root:not\(\[data-motion="full"\]\) \.ds-spin/,
    );
    expect(css).toMatch(/:root:not\(\[data-motion="full"\]\) \.ds-breathe/);
    expect(css).toMatch(
      /:root:not\(\[data-motion="full"\]\)\s+\.ds-progress__track--indeterminate\s+\.ds-progress__bar/,
    );
  });
});
