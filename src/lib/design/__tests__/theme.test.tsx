import {
  setupDom,
  click,
  mockMatchMedia,
  render,
} from "@/components/ds/__tests__/dom";
import { afterEach, describe, expect, test } from "bun:test";
import { act } from "react";
import {
  THEME_STORAGE_KEY,
  applyTheme,
  normalizeThemePreference,
  readThemePreference,
  resolveTheme,
  useThemePreference,
  writeThemePreference,
} from "../theme";
import { prefersReducedMotion, usePrefersReducedMotion } from "../motion";

setupDom();

afterEach(() => {
  try {
    window.localStorage.clear();
  } catch {
    /* storage may be the throwing stub below */
  }
});

describe("theme preference", () => {
  test("normalises unknown values to system and resolves", () => {
    expect(normalizeThemePreference("dark")).toBe("dark");
    expect(normalizeThemePreference("purple")).toBe("system");
    expect(normalizeThemePreference(null)).toBe("system");
    expect(resolveTheme("system", true)).toBe("dark");
    expect(resolveTheme("system", false)).toBe("light");
    expect(resolveTheme("light", true)).toBe("light");
  });

  test("persists in localStorage; system removes the key", () => {
    expect(readThemePreference()).toBe("system");
    expect(writeThemePreference("dark")).toBe(true);
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");
    expect(readThemePreference()).toBe("dark");
    writeThemePreference("system");
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBeNull();
  });

  test("a throwing storage falls back to system and never throws", () => {
    const descriptor = Object.getOwnPropertyDescriptor(window, "localStorage");
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      get() {
        throw new Error("SecurityError");
      },
    });
    try {
      expect(readThemePreference()).toBe("system");
      expect(writeThemePreference("dark")).toBe(false);
    } finally {
      if (descriptor) Object.defineProperty(window, "localStorage", descriptor);
    }
  });

  test("applyTheme sets or removes data-theme on the given element only", () => {
    const element = document.createElement("div");
    applyTheme(element, "dark");
    expect(element.getAttribute("data-theme")).toBe("dark");
    applyTheme(element, "system");
    expect(element.hasAttribute("data-theme")).toBe(false);
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
  });

  test("the hook reads, writes and follows the system", async () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, "light");
    const restore = mockMatchMedia(["prefers-color-scheme: dark"]);
    let state: ReturnType<typeof useThemePreference> | null = null;
    function Probe() {
      state = useThemePreference();
      return (
        <button type="button" onClick={() => state!.setPreference("system")}>
          {state.resolved}
        </button>
      );
    }
    try {
      const { host } = await render(<Probe />);
      expect(state!.preference).toBe("light");
      expect(host.textContent).toBe("light");
      await click(host.querySelector("button"));
      expect(state!.preference).toBe("system");
      expect(host.textContent).toBe("dark");
      expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBeNull();
      // Another tab changes it.
      window.localStorage.setItem(THEME_STORAGE_KEY, "light");
      await act(async () => {
        window.dispatchEvent(
          new StorageEvent("storage", { key: THEME_STORAGE_KEY }),
        );
      });
      expect(host.textContent).toBe("light");
      // The hook never touches <html>: existing pages stay as they are.
      expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
    } finally {
      restore();
    }
  });
});

describe("reduced motion", () => {
  test("reads the media query and the data-motion attribute", async () => {
    const restore = mockMatchMedia([]);
    try {
      const wrapper = document.createElement("div");
      wrapper.setAttribute("data-motion", "reduce");
      const inner = document.createElement("span");
      wrapper.appendChild(inner);
      expect(prefersReducedMotion()).toBe(false);
      expect(prefersReducedMotion(inner)).toBe(true);
    } finally {
      restore();
    }
    const restoreReduced = mockMatchMedia(["prefers-reduced-motion"]);
    try {
      let value = false;
      function Probe() {
        value = usePrefersReducedMotion();
        return null;
      }
      await render(<Probe />);
      expect(value).toBe(true);
    } finally {
      restoreReduced();
    }
  });
});
