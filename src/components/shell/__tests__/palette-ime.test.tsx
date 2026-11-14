import { press, render, setupDom } from "@/components/ds/__tests__/dom";
import { describe, expect, mock, test } from "bun:test";
import { act } from "react";

const pushed: string[] = [];
mock.module("next/navigation", () => ({
  useRouter: () => ({ push: (href: string) => pushed.push(href) }),
  usePathname: () => "/pool",
}));
const { ShellProvider } = await import("../shell-context");
const { CommandPalette } = await import("../command-palette");
const { navPages } = await import("../commands");
const { isImeEvent } = await import("../global-keys");

setupDom();

const open = () => Boolean(document.querySelector(".levi-palette"));
async function openPalette() {
  await render(
    <ShellProvider>
      <CommandPalette pages={navPages({ live: false, pool: true })} />
    </ShellProvider>,
  );
  await press(document.body, "k", { ctrlKey: true });
  const search = document.activeElement as HTMLInputElement;
  await act(async () => {
    Object.getOwnPropertyDescriptor(
      HTMLInputElement.prototype,
      "value",
    )!.set!.call(search, "guide");
    search.dispatchEvent(new Event("input", { bubbles: true }));
    search.dispatchEvent(
      new KeyboardEvent("keyup", { bubbles: true, key: "e" }),
    );
  });
  return search;
}

describe("input-method keys in the palette", () => {
  test("isImeEvent: composing or keyCode 229, one definition", () => {
    expect(isImeEvent({ isComposing: true })).toBe(true);
    expect(isImeEvent({ keyCode: 229 })).toBe(true);
    expect(isImeEvent({ isComposing: false, keyCode: 13 })).toBe(false);
    expect(isImeEvent({})).toBe(false);
  });

  test("Enter that ends a composition (isComposing, or keyCode 229) runs nothing", async () => {
    pushed.length = 0;
    const search = await openPalette();
    await press(search, "Enter", { isComposing: true });
    await press(search, "Enter", { keyCode: 229 });
    expect(pushed).toEqual([]);
    expect(open()).toBe(true);
    await press(search, "Enter");
    expect(pushed).toEqual(["/guide"]);
  });

  test("Escape during composition does not close the palette; a plain Escape does", async () => {
    const search = await openPalette();
    await press(search, "Escape", { isComposing: true });
    await press(search, "Escape", { keyCode: 229 });
    expect(open()).toBe(true);
    await press(search, "Escape");
    expect(open()).toBe(false);
  });
});
