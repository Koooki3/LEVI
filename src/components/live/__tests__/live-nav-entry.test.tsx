import { act } from "react";
import { click, render, setupDom } from "@/components/ds/__tests__/dom";
import { describe, expect, mock, test } from "bun:test";
import { readFileSync } from "fs";
import { join } from "path";
import { commandsIn } from "../command-text";
import { disabledText } from "../embedding";

const state = {
  enabled: null as boolean | null,
};
mock.module("../use-live-pulse", () => ({
  useLivePulse: () => ({
    enabled: state.enabled,
    embedded: false,
    pulse: { light: "grey", reason: "unreachable", count: 0 },
  }),
}));
mock.module("next/link", () => ({
  default: ({ children, ...props }: Record<string, unknown>) => (
    <a {...(props as object)}>{children as never}</a>
  ),
}));

const { LiveNavLink } = await import("../live-nav");
const { CommandText, CopyCommandButton } = await import("../command-text");
const { navPages } = await import("@/components/shell/commands");

setupDom();

describe("the live entry of the navigation", () => {
  test("is a plain link when there is no live service, so the page can be found", async () => {
    for (const enabled of [false, null]) {
      state.enabled = enabled;
      const { host } = await render(<LiveNavLink current />);
      const link = host.querySelector("a")!;
      expect(link.getAttribute("href")).toBe("/live");
      expect(link.textContent).toBe("Live evaluation");
      expect(link.getAttribute("aria-current")).toBe("page");
      // No status dot or count without a service.
      expect(host.querySelector(".levi-pulse-dot")).toBeNull();
      expect(host.querySelector(".levi-pulse-count")).toBeNull();
    }
  });

  test("carries the service's state when there is one", async () => {
    state.enabled = true;
    const { host } = await render(<LiveNavLink />);
    expect(host.querySelector(".levi-pulse-dot")).toBeTruthy();
  });

  test("the header offers it whatever the service state", () => {
    const header = readFileSync(
      join(import.meta.dir, "../../levi-header.tsx"),
      "utf8",
    );
    expect(header).toMatch(/navPages\(\{ live: true/);
    expect(navPages({ live: true, pool: false })[0].href).toBe("/live");
  });
});

describe("commands in the empty state", () => {
  test("a backticked command is code, with no backticks showing", async () => {
    const body = disabledText(undefined).body;
    expect(commandsIn(body)).toEqual(["levi live start"]);
    const { host } = await render(<CommandText text={body} />);
    expect(host.querySelector("code")?.textContent).toBe("levi live start");
    expect(host.textContent).not.toContain("`");
    expect(commandsIn(disabledText("not_live").body)[0]).toBe(
      "levi live start",
    );
    expect(commandsIn(disabledText("product_workspace").body)).toContain(
      "levi live start --workspace <folder>",
    );
  });

  test("Copy command writes it to the clipboard", async () => {
    const written: string[] = [];
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: async (text: string) => void written.push(text) },
    });
    const { host } = await render(
      <CopyCommandButton command="levi live start" />,
    );
    await click(host.querySelector("button"));
    await act(async () => {});
    expect(written).toEqual(["levi live start"]);
    expect(host.querySelector("button")?.textContent).toContain("Copy command");
  });
});
