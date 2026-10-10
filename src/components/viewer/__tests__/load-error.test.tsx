import { flush, render, setupDom } from "@/components/ds/__tests__/dom";
import { describe, expect, mock, test } from "bun:test";

mock.module("next/navigation", () => ({
  usePathname: () => "/local/x/episode_abc",
  useRouter: () => ({ push: () => {}, replace: () => {} }),
}));
const { EpisodeLoadError } = await import("../load-error");
const { RouteTitle } = await import("@/components/shell/route-title");

setupDom();

describe("the episode viewer's error page", () => {
  test("is a main landmark, and names the tab when the address is no episode", async () => {
    const { host } = await render(
      <>
        <RouteTitle />
        <EpisodeLoadError message="bad index" onRetry={() => {}} />
      </>,
    );
    await flush();
    expect(host.querySelectorAll("main").length).toBe(1);
    expect(host.querySelector("main [role=alert]")).toBeTruthy();
    expect(document.title).toBe("This episode could not be loaded · LEVI");
  });
});

const { LocaleProvider } = await import("@/components/levi-locale");
const BLOCKED =
  "Cannot read the local dataset lvsec: the LEVI file service answered 421.";

async function shown(message: string, language: "en" | "zh") {
  localStorage.setItem("levi-language", language);
  const { host } = await render(
    <LocaleProvider>
      <EpisodeLoadError message={message} onRetry={() => {}} />
    </LocaleProvider>,
  );
  await flush();
  localStorage.removeItem("levi-language");
  return host.querySelector("main")?.textContent ?? "";
}

describe("a request the web bridge blocked", () => {
  test("says why and what to do, not 'sign in to Hugging Face'", async () => {
    for (const message of [
      BLOCKED,
      "Request blocked: this LEVI page does not answer to the address it was opened under. Open it as http://127.0.0.1:7860 (or localhost), or add the name to LEVI_UI_ALLOWED_HOSTS.",
    ]) {
      const text = await shown(message, "en");
      expect(text).toContain("Request blocked");
      expect(text).toContain("LEVI_UI_ALLOWED_HOSTS");
      expect(text).toContain("http://127.0.0.1:7860");
      expect(text).not.toContain("Hugging Face");
    }
  });

  test("in Chinese too", async () => {
    const text = await shown(BLOCKED, "zh");
    expect(text).toContain("请求被拦截");
    expect(text).toContain("LEVI_UI_ALLOWED_HOSTS");
    expect(text).not.toContain("Hugging Face");
  });

  test("a write the bridge refused says so as well", async () => {
    const text = await shown(
      "Request blocked: writes through the web UI must come from the LEVI page itself. Scripts use the levi CLI or a scoped agent token.",
      "zh",
    );
    expect(text).toContain("请求被拦截");
    expect(text).not.toContain("Hugging Face");
  });

  test("other failures keep the general explanation", async () => {
    const text = await shown("Failed to fetch JSON x: 404 Not Found", "en");
    expect(text).toContain("sign in to Hugging Face");
    expect(text).not.toContain("Request blocked");
  });
});
