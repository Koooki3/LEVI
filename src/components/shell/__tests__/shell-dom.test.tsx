import {
  click,
  flush,
  focus,
  press,
  render,
  setupDom,
} from "@/components/ds/__tests__/dom";
import { describe, expect, mock, test } from "bun:test";
import { act } from "react";

const pushed: string[] = [];
const route = { path: "/pool" };
mock.module("next/navigation", () => ({
  useRouter: () => ({ push: (href: string) => pushed.push(href) }),
  usePathname: () => route.path,
}));

const { ConfirmProvider, useConfirmAction } = await import("../confirm");
const { ShellProvider, useShell } = await import("../shell-context");
const { CommandPalette } = await import("../command-palette");
const { ShortcutsDialog } = await import("../shortcuts-dialog");
const { navPages } = await import("../commands");
const { SHELL_EVENTS } = await import("../shell-events");
const { JobsMenu } = await import("../jobs-menu");
const { JOBS_POLL_MS } = await import("../jobs");

setupDom();

/**
 * happy-dom has no `:modal`; make an open <dialog> opened by showModal()
 * match it, as in a browser, for the duration of `run`.
 */
async function withModalSupport(run: () => Promise<void>) {
  const original = Element.prototype.matches;
  Element.prototype.matches = function (this: Element, selector: string) {
    if (selector === ":modal")
      return this instanceof HTMLDialogElement && this.open;
    return original.call(this, selector);
  };
  try {
    await run();
  } finally {
    Element.prototype.matches = original;
  }
}

/**
 * The call style every former `window.confirm` site uses:
 *   if (!(await confirm({...}))) return;  action();
 */
function Guarded({ log }: { log: string[] }) {
  const confirm = useConfirmAction();
  return (
    <button
      type="button"
      id="danger"
      onClick={async () => {
        if (
          !(await confirm({
            title: "Delete model seg-v3?",
            confirmLabel: "Delete",
            tone: "danger",
          }))
        )
          return;
        log.push("deleted");
      }}
    >
      Delete…
    </button>
  );
}

/**
 * Type into a focused text field. React DOM, loaded under happy-dom, takes
 * its change events from key events (its input-event check fails there), so
 * set the value past React's tracker and send a keyup.
 */
async function type(field: HTMLInputElement, text: string) {
  await act(async () => {
    Object.getOwnPropertyDescriptor(
      HTMLInputElement.prototype,
      "value",
    )!.set!.call(field, text);
    field.dispatchEvent(new Event("input", { bubbles: true }));
    field.dispatchEvent(
      new KeyboardEvent("keyup", { bubbles: true, key: "e" }),
    );
  });
}

const buttonNamed = (name: string) =>
  Array.from(document.querySelectorAll("button")).find(
    (b) => b.textContent === name,
  ) as HTMLButtonElement | undefined;

describe("confirmation instead of window.confirm", () => {
  test("Cancel does not run the action; focus starts on Cancel", async () => {
    const log: string[] = [];
    await render(
      <ConfirmProvider>
        <Guarded log={log} />
      </ConfirmProvider>,
    );
    await focus(document.querySelector("#danger"));
    await click(document.querySelector("#danger"));
    const dialog = document.querySelector('[role="alertdialog"]')!;
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    expect(dialog.textContent).toContain("Delete model seg-v3?");
    expect(document.activeElement?.textContent).toBe("Cancel");
    await click(buttonNamed("Cancel")!);
    await flush();
    expect(log).toEqual([]);
    expect(Boolean(document.querySelector('[role="alertdialog"]'))).toBe(false);
    expect(document.activeElement?.id).toBe("danger");
  });

  test("Escape does not run the action", async () => {
    const log: string[] = [];
    await render(
      <ConfirmProvider>
        <Guarded log={log} />
      </ConfirmProvider>,
    );
    await click(document.querySelector("#danger"));
    await press(document.activeElement, "Escape");
    await flush();
    expect(log).toEqual([]);
  });

  test("the confirm button runs it once", async () => {
    const log: string[] = [];
    await render(
      <ConfirmProvider>
        <Guarded log={log} />
      </ConfirmProvider>,
    );
    await click(document.querySelector("#danger"));
    await click(buttonNamed("Delete")!);
    await flush();
    expect(log).toEqual(["deleted"]);
    expect(Boolean(document.querySelector('[role="alertdialog"]'))).toBe(false);
  });

  test("outside a provider the answer is no", async () => {
    const log: string[] = [];
    await render(<Guarded log={log} />);
    await click(document.querySelector("#danger"));
    await flush();
    expect(log).toEqual([]);
    expect(Boolean(document.querySelector('[role="alertdialog"]'))).toBe(false);
  });
});

describe("an open confirmation keeps the page's keys", () => {
  test("arrows, Space, Ctrl+Z/S and Escape do not reach window listeners", async () => {
    const seen: string[] = [];
    const onKey = (event: KeyboardEvent) =>
      seen.push(`${event.ctrlKey ? "Ctrl+" : ""}${event.key}`);
    window.addEventListener("keydown", onKey);
    try {
      await render(
        <ConfirmProvider>
          <Guarded log={[]} />
        </ConfirmProvider>,
      );
      await focus(document.querySelector("#danger"));
      await click(document.querySelector("#danger"));
      const inside = document.activeElement;
      expect(inside?.textContent).toBe("Cancel");
      await press(inside, "ArrowDown");
      await press(inside, " ");
      await press(inside, "z", { ctrlKey: true });
      await press(inside, "s", { ctrlKey: true });
      expect(seen).toEqual([]);
      await press(inside, "Escape");
      expect(seen).toEqual([]);
      expect(Boolean(document.querySelector('[role="alertdialog"]'))).toBe(
        false,
      );
      // Closed: the page has its keys again.
      await press(document.querySelector("#danger"), "ArrowDown");
      expect(seen).toEqual(["ArrowDown"]);
    } finally {
      window.removeEventListener("keydown", onKey);
    }
  });

  test("a page change answers an open question no", async () => {
    let answer: Promise<boolean> | null = null;
    function Ask() {
      const confirm = useConfirmAction();
      return (
        <button
          type="button"
          id="ask"
          onClick={() => {
            answer = confirm({ title: "Delete?", confirmLabel: "Delete" });
          }}
        >
          Ask
        </button>
      );
    }
    route.path = "/pool";
    const { rerender } = await render(
      <ConfirmProvider>
        <Ask />
      </ConfirmProvider>,
    );
    await click(document.querySelector("#ask"));
    expect(Boolean(document.querySelector('[role="alertdialog"]'))).toBe(true);
    route.path = "/explore";
    await rerender(
      <ConfirmProvider>
        <Ask />
      </ConfirmProvider>,
    );
    expect(await answer!).toBe(false);
    expect(Boolean(document.querySelector('[role="alertdialog"]'))).toBe(false);
    route.path = "/pool";
  });
});

describe("confirmation inside a native modal dialog", () => {
  test("is rendered inside the open showModal() dialog and still answers", async () => {
    await withModalSupport(async () => {
      const log: string[] = [];
      await render(
        <ConfirmProvider>
          <dialog id="push">
            <Guarded log={log} />
          </dialog>
        </ConfirmProvider>,
      );
      const native = document.querySelector<HTMLDialogElement>("#push")!;
      await act(async () => native.showModal());
      await focus(document.querySelector("#danger"));
      await click(document.querySelector("#danger"));
      const question = document.querySelector('[role="alertdialog"]')!;
      expect(Boolean(question)).toBe(true);
      // Inside the native dialog: the rest of the page is inert.
      expect(native.contains(question)).toBe(true);
      await click(buttonNamed("Delete")!);
      await flush();
      expect(log).toEqual(["deleted"]);
      expect(document.activeElement?.id).toBe("danger");
    });
  });

  test("without an open modal dialog it is rendered at the root", async () => {
    await withModalSupport(async () => {
      await render(
        <ConfirmProvider>
          <dialog id="closed">x</dialog>
          <Guarded log={[]} />
        </ConfirmProvider>,
      );
      await click(document.querySelector("#danger"));
      const question = document.querySelector('[role="alertdialog"]')!;
      expect(document.querySelector("#closed")!.contains(question)).toBe(false);
    });
  });
});

function Frame({ children }: { children?: React.ReactNode }) {
  return (
    <ShellProvider>
      <button type="button" id="before">
        Before
      </button>
      <input id="field" />
      <CommandPalette pages={navPages({ live: false, pool: true })} />
      <ShortcutsDialog />
      {children}
    </ShellProvider>
  );
}

const ctrlK = { ctrlKey: true };

describe("theme default", () => {
  test("no stored value: the frame follows the system (no data-theme)", async () => {
    window.localStorage.removeItem("levi-theme");
    document.documentElement.setAttribute("data-theme", "dark");
    let theme = "";
    function Probe() {
      theme = useShell().theme;
      return null;
    }
    await render(
      <Frame>
        <Probe />
      </Frame>,
    );
    expect(theme).toBe("system");
    expect(document.documentElement.getAttribute("data-theme")).toBeNull();
  });
});

describe("theme on first render", () => {
  test("a stored light choice is never overwritten with the default", async () => {
    window.localStorage.setItem("levi-theme", "light");
    const root = document.documentElement;
    const written: string[] = [];
    const original = root.setAttribute.bind(root);
    root.setAttribute = (name: string, value: string) => {
      if (name === "data-theme") written.push(value);
      original(name, value);
    };
    try {
      await render(<Frame />);
      expect(written).not.toContain("dark");
      expect(root.getAttribute("data-theme")).toBe("light");
    } finally {
      root.setAttribute = original;
      window.localStorage.removeItem("levi-theme");
      root.removeAttribute("data-theme");
    }
  });
});

describe("command palette", () => {
  test("Ctrl+K opens it on the search field; Escape closes and returns focus", async () => {
    await render(<Frame />);
    const before = document.querySelector<HTMLButtonElement>("#before")!;
    await focus(before);
    await press(before, "k", ctrlK);
    const dialog = document.querySelector('[role="dialog"]')!;
    expect(dialog.className).toContain("levi-palette");
    const search = dialog.querySelector('[role="combobox"]')!;
    expect(document.activeElement).toBe(search);
    expect(dialog.querySelectorAll('[role="option"]').length).toBeGreaterThan(
      8,
    );
    await press(document.activeElement, "Escape");
    expect(Boolean(document.querySelector('[role="dialog"]'))).toBe(false);
    expect(document.activeElement).toBe(before);
  });

  // Booleans, not elements: printing a happy-dom element in a failure
  // message can exhaust memory and crash Bun instead of failing.
  test("Ctrl+K again closes it; from the shortcut list it switches to it", async () => {
    await render(<Frame />);
    const before = document.querySelector<HTMLButtonElement>("#before")!;
    await focus(before);
    await press(before, "k", ctrlK);
    expect(Boolean(document.querySelector(".levi-palette"))).toBe(true);
    await press(document.activeElement, "k", ctrlK);
    expect(Boolean(document.querySelector(".levi-palette"))).toBe(false);
    expect(document.activeElement === before).toBe(true);
    await press(before, "?", { shiftKey: true });
    expect(Boolean(document.querySelector(".levi-shortcuts"))).toBe(true);
    await press(document.activeElement, "k", ctrlK);
    expect(Boolean(document.querySelector(".levi-shortcuts"))).toBe(false);
    expect(Boolean(document.querySelector(".levi-palette"))).toBe(true);
    // Other keys still stay inside.
    const seen: string[] = [];
    const onKey = (event: KeyboardEvent) => seen.push(event.key);
    window.addEventListener("keydown", onKey);
    await press(document.activeElement, "ArrowDown");
    window.removeEventListener("keydown", onKey);
    expect(seen).toEqual([]);
  });

  test("type to filter, arrows move, Enter goes to the page", async () => {
    pushed.length = 0;
    await render(<Frame />);
    await press(document.body, "k", ctrlK);
    const search = document.activeElement as HTMLInputElement;
    await type(search, "guide");
    const options = document.querySelectorAll('[role="option"]');
    expect(options[0].textContent).toContain("Guide");
    expect(options[0].getAttribute("aria-selected")).toBe("true");
    expect(search.getAttribute("aria-activedescendant")).toBe(options[0].id);
    await press(search, "Enter");
    expect(pushed).toEqual(["/guide"]);
    expect(Boolean(document.querySelector('[role="dialog"]'))).toBe(false);
  });

  test("runs frame actions: the Agent Workbench and the theme", async () => {
    const events: string[] = [];
    const onToggle = () => events.push("toggle");
    window.addEventListener(SHELL_EVENTS.agentToggle, onToggle);
    let theme = "";
    function Probe() {
      theme = useShell().theme;
      return null;
    }
    await render(
      <Frame>
        <Probe />
      </Frame>,
    );
    await press(document.body, "k", ctrlK);
    // The panels follow the pages: Home + 5 pages, then Agent Workbench.
    const options = Array.from(document.querySelectorAll('[role="option"]'));
    const agentIndex = options.findIndex((o) =>
      o.textContent?.includes("Agent Workbench"),
    );
    for (let i = 0; i < agentIndex; i += 1)
      await press(document.activeElement, "ArrowDown");
    await press(document.activeElement, "Enter");
    expect(events).toEqual(["toggle"]);
    window.removeEventListener(SHELL_EVENTS.agentToggle, onToggle);

    await press(document.body, "k", ctrlK);
    const dark = Array.from(document.querySelectorAll('[role="option"]')).find(
      (o) => o.textContent?.includes("Theme: Dark"),
    )!;
    await click(dark);
    expect(theme).toBe("dark");
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
    // Back to "system": the attribute goes, the tokens follow the system.
    await press(document.body, "k", ctrlK);
    await click(
      Array.from(document.querySelectorAll('[role="option"]')).find((o) =>
        o.textContent?.includes("Theme: System"),
      )!,
    );
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
  });

  test("another modal keeps the keys", async () => {
    await render(
      <ConfirmProvider>
        <Frame>
          <Guarded log={[]} />
        </Frame>
      </ConfirmProvider>,
    );
    await click(document.querySelector("#danger"));
    await press(document.activeElement, "k", ctrlK);
    expect(Boolean(document.querySelector(".levi-palette"))).toBe(false);
  });
});

describe("a native modal dialog keeps the keys", () => {
  // Only the frame's state is checked, not the palette itself: a palette
  // opened behind a native dialog would fight it for focus, and under
  // happy-dom that loops without end instead of failing.
  test("Ctrl+K and ? do nothing while a showModal() dialog is open", async () => {
    let state = { palette: false, shortcuts: false };
    function Probe() {
      const shell = useShell();
      state = { palette: shell.paletteOpen, shortcuts: shell.shortcutsOpen };
      return null;
    }
    await withModalSupport(async () => {
      await render(
        <ShellProvider>
          <Probe />
          <dialog id="native">
            <button type="button" id="in-native">
              In
            </button>
          </dialog>
        </ShellProvider>,
      );
      const native = document.querySelector<HTMLDialogElement>("#native")!;
      await act(async () => native.showModal());
      const inside = document.querySelector("#in-native");
      await press(inside, "k", { ctrlKey: true });
      await press(inside, "?", { shiftKey: true });
      expect(state).toEqual({ palette: false, shortcuts: false });
      await act(async () => native.close());
      await press(document.body, "k", { ctrlKey: true });
      expect(state.palette).toBe(true);
    });
  });
});

describe("jobs entry", () => {
  test("asks on load and on opening the menu, never twice at once, 60 s apart", async () => {
    expect(JOBS_POLL_MS).toBe(60_000);
    const asked: string[] = [];
    const pending: Array<() => void> = [];
    const original = globalThis.fetch;
    globalThis.fetch = ((url: string) => {
      asked.push(String(url));
      return new Promise<Response>((resolve) =>
        pending.push(() =>
          resolve(
            new Response(
              String(url).includes("pool")
                ? JSON.stringify({ jobs: [{ status: "running" }] })
                : JSON.stringify([{ status: "running" }, { status: "done" }]),
            ),
          ),
        ),
      );
    }) as unknown as typeof fetch;
    try {
      await render(<JobsMenu pool />);
      expect(asked).toEqual(["/api/levi/pool/jobs", "/api/levi/jobs"]);
      const trigger = document.querySelector<HTMLButtonElement>(
        'button[aria-haspopup="menu"]',
      )!;
      // Still waiting: opening the menu does not ask again.
      await click(trigger);
      expect(asked).toHaveLength(2);
      await click(trigger); // close
      await act(async () => {
        pending.splice(0).forEach((answer) => answer());
        await new Promise((r) => setTimeout(r, 0));
      });
      expect(
        trigger.querySelector(".ds-menu-trigger__badge")!.textContent,
      ).toBe("2");
      // Answered: opening the menu asks once more.
      await click(trigger);
      expect(asked).toHaveLength(4);
      await click(trigger); // close
      // Coming back to the tab asks at once, but not while still waiting.
      const visible = () =>
        act(async () => {
          document.dispatchEvent(new Event("visibilitychange"));
        });
      await visible();
      expect(asked).toHaveLength(4);
      await act(async () => {
        pending.splice(0).forEach((answer) => answer());
        await new Promise((r) => setTimeout(r, 0));
      });
      await visible();
      expect(asked).toHaveLength(6);
    } finally {
      globalThis.fetch = original;
    }
  });
});

describe("shortcut list", () => {
  test("? opens it, not while typing in a field", async () => {
    await render(<Frame />);
    const field = document.querySelector<HTMLInputElement>("#field")!;
    await focus(field);
    await press(field, "?", { shiftKey: true });
    expect(Boolean(document.querySelector(".levi-shortcuts"))).toBe(false);
    const before = document.querySelector<HTMLButtonElement>("#before")!;
    await focus(before);
    await press(before, "?", { shiftKey: true });
    const dialog = document.querySelector(".levi-shortcuts")!;
    expect(dialog.getAttribute("role")).toBe("dialog");
    expect(dialog.textContent).toContain("Open the command palette");
    expect(dialog.querySelectorAll("kbd").length).toBeGreaterThan(5);
    await press(document.activeElement, "Escape");
    expect(Boolean(document.querySelector(".levi-shortcuts"))).toBe(false);
    expect(document.activeElement).toBe(before);
  });
});
