/**
 * A DOM for component tests (happy-dom), registered when this module is
 * evaluated so React DOM sees `window` when it loads. Import it FIRST in a
 * component test and call `setupDom()`; after the file the globals are
 * removed again, so tests in other files keep running without a DOM.
 */
import "./dom-register";
import { GlobalRegistrator } from "@happy-dom/global-registrator";
import { afterAll, afterEach, beforeAll } from "bun:test";
import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";

const mounted: Array<{ root: Root; host: HTMLElement }> = [];

export async function render(node: ReactNode): Promise<{
  host: HTMLElement;
  rerender: (next: ReactNode) => Promise<void>;
}> {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  mounted.push({ root, host });
  await act(async () => root.render(node));
  return {
    host,
    rerender: async (next) => {
      await act(async () => root.render(next));
    },
  };
}

export async function press(
  target: Element | null,
  key: string,
  init: KeyboardEventInit = {},
): Promise<void> {
  if (!target) throw new Error(`no element to press ${key} on`);
  await act(async () => {
    target.dispatchEvent(
      new KeyboardEvent("keydown", {
        key,
        bubbles: true,
        cancelable: true,
        ...init,
      }),
    );
  });
}

export async function click(target: Element | null): Promise<void> {
  if (!target) throw new Error("no element to click");
  await act(async () => {
    (target as HTMLElement).click();
  });
}

export async function fire(
  target: Element | null,
  event: Event,
): Promise<void> {
  if (!target) throw new Error(`no element for ${event.type}`);
  await act(async () => {
    target.dispatchEvent(event);
  });
}

export async function focus(target: Element | null): Promise<void> {
  if (!target) throw new Error("no element to focus");
  await act(async () => {
    (target as HTMLElement).focus();
  });
}

export async function flush(ms = 0): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, ms));
  });
}

/** Make `matchMedia` answer `matches` for queries containing `fragment`. */
export function mockMatchMedia(matching: string[]): () => void {
  const original = window.matchMedia;
  window.matchMedia = ((query: string) =>
    ({
      matches: matching.some((fragment) => query.includes(fragment)),
      media: query,
      onchange: null,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
      addListener: () => undefined,
      removeListener: () => undefined,
      dispatchEvent: () => false,
    }) as unknown as MediaQueryList) as typeof window.matchMedia;
  return () => {
    window.matchMedia = original;
  };
}

/**
 * Call once at the top of every component test file. Hooks are scoped to the
 * calling file (a module is evaluated once per run, so they cannot live at
 * this module's top level): register the DOM before the file's tests,
 * unmount after each test, and remove the globals after the file.
 */
export function setupDom(): void {
  beforeAll(() => {
    if (!GlobalRegistrator.isRegistered) GlobalRegistrator.register();
    (
      globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }
    ).IS_REACT_ACT_ENVIRONMENT = true;
  });
  afterEach(async () => {
    while (mounted.length) {
      const { root, host } = mounted.pop()!;
      await act(async () => root.unmount());
      host.remove();
    }
    document.body.innerHTML = "";
  });
  afterAll(async () => {
    if (GlobalRegistrator.isRegistered) await GlobalRegistrator.unregister();
  });
}
