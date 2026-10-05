import { flush, render, setupDom } from "@/components/ds/__tests__/dom";
import { describe, expect, mock, test } from "bun:test";
import { act } from "react";

const route = { path: "/explore" };
mock.module("next/navigation", () => ({
  useRouter: () => ({ push: () => {}, replace: () => {} }),
  usePathname: () => route.path,
}));

const { RouteTitle } = await import("../route-title");
const { LocaleProvider } = await import("@/components/levi-locale");

setupDom();

describe("RouteTitle in a page", () => {
  test("writes the page's name and keeps it when the framework writes its own", async () => {
    const framework = document.createElement("title");
    framework.textContent = "LEVI";
    document.body.appendChild(framework);
    await render(
      <LocaleProvider>
        <RouteTitle />
      </LocaleProvider>,
    );
    expect(document.title).toBe("Explore · LEVI");
    // The metadata's title lands after hydration, and again on a route change.
    await act(async () => {
      framework.textContent = "LEVI";
    });
    await flush();
    expect(document.title).toBe("Explore · LEVI");
    // The element itself may be replaced.
    await act(async () => {
      framework.remove();
      const next = document.createElement("title");
      next.textContent = "Something else";
      document.body.appendChild(next);
    });
    await flush();
    expect(document.title).toBe("Explore · LEVI");
  });
});
