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
mock.module("next/navigation", () => ({
  useRouter: () => ({ push: (href: string) => pushed.push(href) }),
  usePathname: () => "/pool",
}));

const { ConfirmProvider, useConfirmAction } = await import("../confirm");
const { ShellProvider, useShell } = await import("../shell-context");
const { CommandPalette } = await import("../command-palette");
const { ShortcutsDialog } = await import("../shortcuts-dialog");
const { navPages } = await import("../commands");
const { SHELL_EVENTS } = await import("../shell-events");

setupDom();

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
    expect(document.querySelector('[role="alertdialog"]')).toBeNull();
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
    expect(document.querySelector('[role="alertdialog"]')).toBeNull();
  });

  test("outside a provider the answer is no", async () => {
    const log: string[] = [];
    await render(<Guarded log={log} />);
    await click(document.querySelector("#danger"));
    await flush();
    expect(log).toEqual([]);
    expect(document.querySelector('[role="alertdialog"]')).toBeNull();
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
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(document.activeElement).toBe(before);
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
    expect(document.querySelector('[role="dialog"]')).toBeNull();
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
    expect(document.querySelector(".levi-palette")).toBeNull();
  });
});

describe("shortcut list", () => {
  test("? opens it, not while typing in a field", async () => {
    await render(<Frame />);
    const field = document.querySelector<HTMLInputElement>("#field")!;
    await focus(field);
    await press(field, "?", { shiftKey: true });
    expect(document.querySelector(".levi-shortcuts")).toBeNull();
    const before = document.querySelector<HTMLButtonElement>("#before")!;
    await focus(before);
    await press(before, "?", { shiftKey: true });
    const dialog = document.querySelector(".levi-shortcuts")!;
    expect(dialog.getAttribute("role")).toBe("dialog");
    expect(dialog.textContent).toContain("Open the command palette");
    expect(dialog.querySelectorAll("kbd").length).toBeGreaterThan(5);
    await press(document.activeElement, "Escape");
    expect(document.querySelector(".levi-shortcuts")).toBeNull();
    expect(document.activeElement).toBe(before);
  });
});
