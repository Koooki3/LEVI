import {
  setupDom,
  click,
  mockMatchMedia,
  render,
} from "@/components/ds/__tests__/dom";
import { afterEach, describe, expect, test } from "bun:test";
import { act } from "react";
import {
  THEME_DEFAULT_PREFERENCE,
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
  test("normalises unknown values to the default and resolves", () => {
    expect(normalizeThemePreference("dark")).toBe("dark");
    expect(normalizeThemePreference("system")).toBe("system");
    expect(normalizeThemePreference("purple")).toBe(THEME_DEFAULT_PREFERENCE);
    expect(normalizeThemePreference(null)).toBe(THEME_DEFAULT_PREFERENCE);
    expect(resolveTheme("system", true)).toBe("dark");
    expect(resolveTheme("system", false)).toBe("light");
    expect(resolveTheme("light", true)).toBe("light");
  });

  test("no stored value follows the system", () => {
    expect(THEME_DEFAULT_PREFERENCE).toBe("system");
    window.localStorage.removeItem(THEME_STORAGE_KEY);
    expect(readThemePreference()).toBe("system");
  });

  test("persists every explicit choice in localStorage, the default included", () => {
    expect(writeThemePreference("light")).toBe(true);
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("light");
    expect(readThemePreference()).toBe("light");
    writeThemePreference("system");
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("system");
    expect(readThemePreference()).toBe("system");
    // Choosing dark (the transitional default) is a choice too: kept, so it
    // still holds when stage 5 makes "system" the default.
    writeThemePreference("dark");
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");
    expect(readThemePreference()).toBe("dark");
  });

  test("a throwing storage falls back to the default and never throws", () => {
    const descriptor = Object.getOwnPropertyDescriptor(window, "localStorage");
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      get() {
        throw new Error("SecurityError");
      },
    });
    try {
      expect(readThemePreference()).toBe(THEME_DEFAULT_PREFERENCE);
      expect(writeThemePreference("light")).toBe(false);
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
      expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("system");
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
