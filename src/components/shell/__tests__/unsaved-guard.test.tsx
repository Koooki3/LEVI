import {
  click,
  flush,
  focus,
  press,
  render,
  setupDom,
} from "@/components/ds/__tests__/dom";
import {
  afterAll,
  afterEach,
  beforeAll,
  describe,
  expect,
  mock,
  test,
} from "bun:test";
import { act, type MouseEvent as ReactMouseEvent } from "react";

// Every way out of the page except a keyboard jump (which has its own tests in
// shell-dom.test.tsx) asks first while work is unsaved: header links, the
// brand, the palette, the Jobs menu, and closing the tab.
const pushed: string[] = [];
mock.module("next/navigation", () => ({
  useRouter: () => ({ push: (href: string) => pushed.push(href) }),
  usePathname: () => "/marvel_workstation",
}));
mock.module("@/components/live/use-live-pulse", () => ({
  useLivePulse: () => ({ enabled: false, embedded: false, pulse: null }),
}));
mock.module("@/components/live/live-nav", () => ({
  LiveNavLink: () => (
    // eslint-disable-next-line @next/next/no-html-link-for-pages
    <a className="levi-live-nav" href="/live">
      Live evaluation
    </a>
  ),
}));

const { AppFrame } = await import("../app-frame");
const { guardLinkClick, goGuarded, setUnsavedWork } =
  await import("../unsaved-work");
const { default: LeviHeader } = await import("../../levi-header");

setupDom();
const realFetch = globalThis.fetch;
beforeAll(() => {
  globalThis.fetch = (async () =>
    new Response("[]", { status: 200 })) as unknown as typeof fetch;
});
afterAll(() => {
  globalThis.fetch = realFetch;
});
afterEach(() => {
  setUnsavedWork(false);
  pushed.length = 0;
});

const dialog = () => document.querySelector('[role="alertdialog"]');
const button = (root: ParentNode, label: string) =>
  Array.from(root.querySelectorAll("button")).find((b) =>
    b.textContent?.includes(label),
  ) ?? null;

async function clickLink(link: Element, init: MouseEventInit = {}) {
  const event = new MouseEvent("click", {
    bubbles: true,
    cancelable: true,
    button: 0,
    ...init,
  });
  await act(async () => {
    link.dispatchEvent(event);
  });
  return event;
}

describe("header links and the brand", () => {
  test("a plain click with unsaved work asks first, for the brand, a page link and the live link", async () => {
    setUnsavedWork(true);
    const { host } = await render(
      <AppFrame>
        <LeviHeader />
      </AppFrame>,
    );
    for (const selector of [
      "a.levi-shell-brand",
      "nav a[href='/explore']",
      "nav a.levi-live-nav",
    ]) {
      const event = await clickLink(host.querySelector(selector)!);
      expect(event.defaultPrevented).toBe(true);
      await flush();
      expect(dialog()?.textContent).toContain(
        "Leave this page without saving?",
      );
      await click(button(dialog()!, "Stay here"));
      await flush();
    }
    expect(pushed).toEqual([]);
    // Leave: the router goes where the link pointed.
    await clickLink(host.querySelector("nav a[href='/explore']")!);
    await flush();
    await click(button(dialog()!, "Leave without saving"));
    await flush();
    expect(pushed).toEqual(["/explore"]);
  });

  test("a new-tab click, another target or a download is left alone; so is a click when nothing is unsaved", () => {
    const anchor = (attrs: Record<string, string>) => {
      const a = document.createElement("a");
      a.setAttribute("href", "/explore");
      for (const [k, v] of Object.entries(attrs)) a.setAttribute(k, v);
      return a;
    };
    const run = (el: Element, init: Partial<MouseEvent> = {}) => {
      let prevented = false;
      guardLinkClick({
        button: 0,
        metaKey: false,
        ctrlKey: false,
        shiftKey: false,
        altKey: false,
        defaultPrevented: false,
        target: el,
        preventDefault: () => (prevented = true),
        stopPropagation: () => undefined,
        ...init,
      } as unknown as ReactMouseEvent);
      return prevented;
    };
    setUnsavedWork(true);
    expect(run(anchor({}), { ctrlKey: true })).toBe(false);
    expect(run(anchor({}), { button: 1 })).toBe(false);
    expect(run(anchor({ target: "_blank" }))).toBe(false);
    expect(run(anchor({ download: "" }))).toBe(false);
    expect(run(anchor({ href: "https://example.org/" }))).toBe(false);
    expect(run(anchor({}))).toBe(true);
    setUnsavedWork(false);
    expect(run(anchor({}))).toBe(false);
  });
});

describe("the command palette", () => {
  test("choosing a page asks first with unsaved work, and goes at once without", async () => {
    await render(
      <AppFrame>
        <LeviHeader />
      </AppFrame>,
    );
    const choose = async () => {
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
      await press(search, "Enter");
      await flush();
    };
    setUnsavedWork(true);
    await choose();
    expect(dialog()).not.toBeNull();
    expect(pushed).toEqual([]);
    await click(button(dialog()!, "Leave without saving"));
    await flush();
    expect(pushed).toEqual(["/guide"]);
    setUnsavedWork(false);
    pushed.length = 0;
    await choose();
    expect(dialog()).toBeNull();
    expect(pushed).toEqual(["/guide"]);
  });
});

describe("goGuarded (the Jobs menu and its toast action)", () => {
  test("pushes directly when nothing is unsaved and asks through the frame otherwise", async () => {
    const direct: string[] = [];
    goGuarded((href) => direct.push(href), "/pool");
    expect(direct).toEqual(["/pool"]);
    await render(<AppFrame>{null}</AppFrame>);
    setUnsavedWork(true);
    goGuarded((href) => direct.push(href), "/workbench");
    await flush();
    expect(direct).toEqual(["/pool"]);
    expect(dialog()).not.toBeNull();
    await click(button(dialog()!, "Stay here"));
  });

  test("the Jobs menu items use it", async () => {
    const { host } = await render(
      <AppFrame>
        <LeviHeader />
      </AppFrame>,
    );
    setUnsavedWork(true);
    const trigger = host.querySelector<HTMLButtonElement>(
      '.levi-shell-actions button[aria-haspopup="menu"]',
    );
    // The Jobs trigger is the first menu button among the actions.
    await focus(trigger);
    await click(trigger);
    const item = document.querySelector("[role^=menuitem]");
    expect(item).not.toBeNull();
    await click(item);
    await flush();
    expect(pushed).toEqual([]);
    expect(dialog()).not.toBeNull();
    await click(button(dialog()!, "Stay here"));
  });
});

describe("closing the tab", () => {
  test("beforeunload is cancelled only while work is unsaved", async () => {
    await render(<AppFrame>{null}</AppFrame>);
    const fire = () => {
      const event = new Event("beforeunload", { cancelable: true });
      window.dispatchEvent(event);
      return event.defaultPrevented;
    };
    expect(fire()).toBe(false);
    setUnsavedWork(true);
    expect(fire()).toBe(true);
    setUnsavedWork(false);
    expect(fire()).toBe(false);
  });
});
