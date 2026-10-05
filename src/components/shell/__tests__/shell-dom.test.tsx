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
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";

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
const { navPages, paletteDatasets, buildCommands, filterCommands } =
  await import("../commands");
const { CHORD_PAGES, chordPage, isChordLeader, shortcutGroups } =
  await import("../global-keys");
const { SHELL_EVENTS } = await import("../shell-events");
const { AppFrame } = await import("../app-frame");
const { setUnsavedWork } = await import("../unsaved-work");
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

const CATALOG = {
  demos: ["lerobot/aloha_static_coffee"],
  local: [
    { id: "local/screws_dev24", name: "screws_dev24", kind: "raw" },
    { id: "bad id", name: "x" },
    null,
  ],
};

function ChordFrame() {
  return (
    <ShellProvider navigate={(href) => pushed.push(href)}>
      <button type="button" id="before">
        Before
      </button>
      <input id="field" />
      <CommandPalette pages={navPages({ live: true, pool: true })} />
      <ShortcutsDialog />
    </ShellProvider>
  );
}

describe("go-to chords (G, then a letter)", () => {
  test("pure: the leader and the pages", () => {
    expect(isChordLeader({ key: "g" })).toBe(true);
    expect(isChordLeader({ key: "G" })).toBe(true);
    expect(isChordLeader({ key: "g", ctrlKey: true })).toBe(false);
    expect(isChordLeader({ key: "g", shiftKey: true })).toBe(false);
    expect(isChordLeader({ key: "g", isComposing: true })).toBe(false);
    expect(chordPage({ key: "e" })).toBe("/explore");
    expect(chordPage({ key: "w" })).toBe("/workbench");
    expect(chordPage({ key: "p" })).toBe("/pool");
    expect(chordPage({ key: "l" })).toBe("/live");
    expect(chordPage({ key: "r" })).toBe("/report");
    expect(chordPage({ key: "h" })).toBe("/");
    expect(chordPage({ key: "u" })).toBe("/guide");
    expect(chordPage({ key: "x" })).toBeNull();
    expect(chordPage({ key: "e", metaKey: true })).toBeNull();
  });

  test("G then E goes to Explore; a wrong second key does nothing", async () => {
    pushed.length = 0;
    await render(<ChordFrame />);
    const before = document.querySelector<HTMLButtonElement>("#before")!;
    await focus(before);
    await press(before, "g");
    await press(before, "e");
    expect(pushed).toEqual(["/explore"]);
    await press(before, "g");
    await press(before, "x");
    await press(before, "e");
    expect(pushed).toEqual(["/explore"]);
    // Every page of the list.
    for (const [letter, page] of Object.entries(CHORD_PAGES)) {
      await press(before, "g");
      await press(before, letter);
      expect(pushed.at(-1)).toBe(page.href);
    }
  });

  test("not while typing in a field, and not too late", async () => {
    pushed.length = 0;
    await render(<ChordFrame />);
    const field = document.querySelector<HTMLInputElement>("#field")!;
    await focus(field);
    await press(field, "g");
    await press(field, "e");
    expect(pushed).toEqual([]);
    const before = document.querySelector<HTMLButtonElement>("#before")!;
    const now = Date.now;
    let at = 1_000_000;
    Date.now = () => at;
    try {
      await press(before, "g");
      at += 1600;
      await press(before, "e");
      expect(pushed).toEqual([]);
    } finally {
      Date.now = now;
    }
  });

  test("the shortcut list names every chord, in both languages", async () => {
    const rows = shortcutGroups(false).find(
      (g) => g.title === "Everywhere",
    )!.rows;
    for (const [letter, page] of Object.entries(CHORD_PAGES)) {
      const row = rows.find(
        (r) => r.keys[0].join("") === `G${letter.toUpperCase()}`,
      );
      expect(row?.label).toBe(`Go to: ${page.label}`);
      expect(row!.label in en).toBe(true);
      expect(row!.label in zh).toBe(true);
    }
  });
});

describe("keyboard jumps and unsaved work", () => {
  test("a jump closes the palette and the list left open behind it", async () => {
    pushed.length = 0;
    let shell: ReturnType<typeof useShell> | null = null;
    function Probe() {
      shell = useShell();
      return null;
    }
    await render(
      <ShellProvider navigate={(href) => pushed.push(href)}>
        <Probe />
      </ShellProvider>,
    );
    await act(async () => shell!.setShortcutsOpen(true));
    expect(shell!.shortcutsOpen).toBe(true);
    await press(document.body, "g");
    await press(document.body, "r");
    expect(pushed).toEqual(["/report"]);
    expect(shell!.shortcutsOpen).toBe(false);
    expect(shell!.paletteOpen).toBe(false);
  });

  test("with an unsaved draft the jump asks first; Stay keeps the page, Leave goes", async () => {
    pushed.length = 0;
    setUnsavedWork(true);
    try {
      await render(
        <AppFrame>
          <button type="button" id="page">
            page
          </button>
        </AppFrame>,
      );
      const page = document.querySelector<HTMLButtonElement>("#page")!;
      await focus(page);
      await press(page, "g");
      await press(page, "e");
      await flush();
      const dialog = document.querySelector('[role="alertdialog"]')!;
      expect(dialog.textContent).toContain("Leave this page without saving?");
      expect(pushed).toEqual([]);
      const button = (label: string) =>
        [...dialog.querySelectorAll("button")].find((b) =>
          b.textContent?.includes(label),
        )!;
      await click(button("Stay here"));
      await flush();
      expect(pushed).toEqual([]);
      await press(page, "g");
      await press(page, "e");
      await flush();
      await click(
        [...document.querySelectorAll('[role="alertdialog"] button')].find(
          (b) => b.textContent?.includes("Leave without saving"),
        ) ?? null,
      );
      await flush();
      expect(pushed).toEqual(["/explore"]);
    } finally {
      setUnsavedWork(false);
    }
  });

  test("with nothing unsaved the jump goes at once", async () => {
    pushed.length = 0;
    setUnsavedWork(false);
    await render(
      <AppFrame>
        <button type="button" id="page">
          page
        </button>
      </AppFrame>,
    );
    const page = document.querySelector<HTMLButtonElement>("#page")!;
    await focus(page);
    await press(page, "g");
    await press(page, "p");
    await flush();
    expect(pushed).toEqual(["/pool"]);
    expect(document.querySelector('[role="alertdialog"]')).toBeNull();
  });
});

describe("the shortcut list", () => {
  test("every row and group has a catalogue entry in both languages, and the viewer's keys are listed", () => {
    const groups = shortcutGroups(false);
    for (const group of groups) {
      expect(group.title in zh).toBe(true);
      for (const row of group.rows) {
        expect(row.label in en).toBe(true);
        expect(row.label in zh).toBe(true);
      }
    }
    const labels = groups.flatMap((g) => g.rows.map((r) => r.label));
    for (const label of [
      "Clear the selected annotation",
      "In a text field, Undo is left to the field",
      "Playhead slider: 0.1 s back or forward",
      "Playhead slider: 1 s back or forward",
      "Playhead slider: start or end",
      "Move within an episode list row",
    ])
      expect(labels).toContain(label);
  });
});

describe("the palette finds datasets", () => {
  test("paletteDatasets keeps the local ones and the demos, skips the rest", () => {
    expect(paletteDatasets(CATALOG)).toEqual([
      { repo: "local/screws_dev24", name: "screws_dev24" },
      {
        repo: "lerobot/aloha_static_coffee",
        name: "lerobot/aloha_static_coffee",
      },
    ]);
    expect(paletteDatasets(null)).toEqual([]);
    expect(paletteDatasets({ local: "x", demos: 3 })).toEqual([]);
    const commands = buildCommands({
      pages: [],
      datasets: paletteDatasets(CATALOG),
      theme: "system",
      language: "en",
      setTheme: () => {},
      setLanguage: () => {},
      toggleAgent: () => {},
      openConnections: () => {},
      openShortcuts: () => {},
    });
    const hit = filterCommands(commands, "screws");
    expect(hit[0].group).toBe("Datasets");
    expect(hit[0].href).toBe("/local/screws_dev24");
  });

  test("typing a dataset name in the palette and pressing Enter opens it", async () => {
    pushed.length = 0;
    const original = globalThis.fetch;
    globalThis.fetch = mock(
      async () =>
        new Response(JSON.stringify(CATALOG), {
          headers: { "content-type": "application/json" },
        }),
    ) as unknown as typeof fetch;
    try {
      await render(<ChordFrame />);
      const before = document.querySelector<HTMLButtonElement>("#before")!;
      await focus(before);
      await press(before, "k", { ctrlKey: true });
      await flush(50);
      const input =
        document.querySelector<HTMLInputElement>('[role="combobox"]')!;
      await type(input, "screws");
      await flush(20);
      const options = [...document.querySelectorAll('[role="option"]')];
      expect(options.map((o) => o.textContent)).toContain("screws_dev24");
      await press(input, "Enter");
      expect(pushed).toEqual(["/local/screws_dev24"]);
    } finally {
      globalThis.fetch = original;
    }
  });
});
