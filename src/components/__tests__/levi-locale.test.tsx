import { render, setupDom } from "@/components/ds/__tests__/dom";
import { act } from "react";
import { flushSync } from "react-dom";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, test } from "bun:test";
import { LocaleProvider, useLocale } from "@/components/levi-locale";

setupDom();
afterEach(() => window.localStorage.clear());

function Probe() {
  const { language } = useLocale();
  return <p id="probe">{language}</p>;
}

describe("LocaleProvider", () => {
  test("the stored language is applied before the first paint", () => {
    window.localStorage.setItem("levi-language", "zh");
    const host = document.createElement("div");
    document.body.appendChild(host);
    const root = createRoot(host);
    const env = globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean };
    env.IS_REACT_ACT_ENVIRONMENT = false; // flushSync is the point here
    // flushSync runs layout effects (and the update they schedule) but not
    // passive effects: with useEffect the DOM would still say "en" here.
    flushSync(() =>
      root.render(
        <LocaleProvider>
          <Probe />
        </LocaleProvider>,
      ),
    );
    const shown = host.querySelector("#probe")?.textContent;
    env.IS_REACT_ACT_ENVIRONMENT = true;
    expect(shown).toBe("zh");
    act(() => root.unmount());
    host.remove();
  });

  test("a storage event from another tab switches the language, and a clear resets it", async () => {
    const { host } = await render(
      <LocaleProvider>
        <Probe />
      </LocaleProvider>,
    );
    const language = () => host.querySelector("#probe")?.textContent;
    expect(language()).toBe("en");
    await act(async () => {
      window.dispatchEvent(
        new StorageEvent("storage", { key: "levi-language", newValue: "zh" }),
      );
    });
    expect(language()).toBe("zh");
    expect(document.documentElement.lang).toBe("zh-CN");
    await act(async () => {
      window.dispatchEvent(
        new StorageEvent("storage", { key: "other-key", newValue: "en" }),
      );
    });
    expect(language()).toBe("zh");
    await act(async () => {
      window.dispatchEvent(new StorageEvent("storage", { key: null }));
    });
    expect(language()).toBe("en");
  });
});
