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
