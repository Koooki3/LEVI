import {
  setupDom,
  click,
  dropFocus,
  flush,
  fire,
  focus,
  press,
  render,
} from "./dom";
import { describe, expect, mock, test } from "bun:test";
import { act, useState } from "react";
import { Trash2 } from "lucide-react";
import { ConfirmDialog, Dialog, Sheet, useConfirm } from "../Dialog";
import { Menu } from "../Menu";
import {
  TOAST_DEFAULT_MS,
  TOAST_MAX_VISIBLE,
  ToastProvider,
  toastDuration,
  useToast,
} from "../Toast";

setupDom();

function DialogHarness({ onClose }: { onClose?: () => void }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" id="opener" onClick={() => setOpen(true)}>
        Open
      </button>
      <Dialog
        open={open}
        onClose={() => {
          onClose?.();
          setOpen(false);
        }}
        title="Rename dataset"
        description="Names are shown in the catalog."
        footer={
          <>
            <button type="button" id="first">
              Cancel
            </button>
            <button type="button" id="last">
              Save
            </button>
          </>
        }
      >
        <input id="name" />
      </Dialog>
    </>
  );
}

describe("Dialog", () => {
  test("is a labelled modal dialog that takes focus", async () => {
    const { host } = await render(<DialogHarness />);
    await focus(host.querySelector("#opener"));
    await click(host.querySelector("#opener"));
    const dialog = host.querySelector('[role="dialog"]')!;
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    const title = host.querySelector(
      `#${CSS.escape(dialog.getAttribute("aria-labelledby")!)}`,
    );
    expect(title!.textContent).toBe("Rename dataset");
    const description = host.querySelector(
      `#${CSS.escape(dialog.getAttribute("aria-describedby")!)}`,
    );
    expect(description!.textContent).toBe("Names are shown in the catalog.");
    // First focusable: the close button in the header.
    expect(document.activeElement?.getAttribute("aria-label")).toBe("Close");
  });

  test("Tab wraps inside, Escape closes, focus returns to the opener", async () => {
    const onClose = mock(() => undefined);
    const { host } = await render(<DialogHarness onClose={onClose} />);
    const opener = host.querySelector<HTMLButtonElement>("#opener")!;
    await focus(opener);
    await click(opener);
    const dialog = host.querySelector('[role="dialog"]')!;
    await focus(host.querySelector("#last"));
    await press(dialog.querySelector("#last"), "Tab");
    expect(document.activeElement?.getAttribute("aria-label")).toBe("Close");
    await press(document.activeElement, "Tab", { shiftKey: true });
    expect(document.activeElement?.id).toBe("last");
    await press(document.activeElement, "Escape");
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(host.querySelector('[role="dialog"]')).toBeNull();
    expect(document.activeElement).toBe(opener);
  });

  test("the scrim closes it; closed renders nothing", async () => {
    const onClose = mock(() => undefined);
    const { host, rerender } = await render(
      <Dialog open onClose={onClose} title="T">
        body
      </Dialog>,
    );
    await click(host.querySelector(".ds-scrim"));
    expect(onClose).toHaveBeenCalledTimes(1);
    await rerender(
      <Dialog open={false} onClose={onClose} title="T">
        body
      </Dialog>,
    );
    expect(host.querySelector(".ds-layer")).toBeNull();
  });

  test("can render into another container (portal)", async () => {
    const target = document.createElement("div");
    target.setAttribute("data-theme", "dark");
    document.body.appendChild(target);
    await render(
      <Dialog open onClose={() => undefined} title="Portal" container={target}>
        x
      </Dialog>,
    );
    expect(target.querySelector('[role="dialog"]')).not.toBeNull();
  });
});

describe("ConfirmDialog and useConfirm", () => {
  test("danger: an alertdialog with focus on Cancel, Escape cancels", async () => {
    const onConfirm = mock(() => undefined);
    const onCancel = mock(() => undefined);
    const { host } = await render(
      <ConfirmDialog
        open
        tone="danger"
        title="Delete model seg-v3?"
        description="It cannot be restored."
        confirmLabel="Delete"
        onConfirm={onConfirm}
        onCancel={onCancel}
      />,
    );
    const dialog = host.querySelector('[role="alertdialog"]')!;
    expect(dialog).not.toBeNull();
    expect(document.activeElement?.textContent).toBe("Cancel");
    const danger = Array.from(dialog.querySelectorAll("button")).find(
      (b) => b.textContent === "Delete",
    )!;
    expect(danger.className).toContain("ds-btn--danger");
    // No close (X) button: the choice is Cancel or the verb.
    expect(dialog.querySelector('[aria-label="Close"]')).toBeNull();
    await press(document.activeElement, "Escape");
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  test("default tone focuses the confirm button", async () => {
    await render(
      <ConfirmDialog
        open
        title="Start export?"
        confirmLabel="Start"
        onConfirm={() => undefined}
        onCancel={() => undefined}
      />,
    );
    expect(document.activeElement?.textContent).toBe("Start");
  });

  test("useConfirm resolves true on confirm and false on cancel", async () => {
    let api: ReturnType<typeof useConfirm> | null = null;
    function Harness() {
      api = useConfirm();
      return <>{api.dialog}</>;
    }
    const { host } = await render(<Harness />);
    let answer: Promise<boolean> | null = null;
    await act(async () => {
      answer = api!.confirm({ title: "Remove?", confirmLabel: "Remove" });
    });
    const confirmButton = Array.from(host.querySelectorAll("button")).find(
      (b) => b.textContent === "Remove",
    );
    await click(confirmButton!);
    expect(await answer!).toBe(true);
    expect(host.querySelector('[role="alertdialog"]')).toBeNull();
    await act(async () => {
      answer = api!.confirm({ title: "Remove?", confirmLabel: "Remove" });
    });
    await press(document.activeElement, "Escape");
    expect(await answer!).toBe(false);
  });
});

describe("useConfirm, superseded", () => {
  test("a second question cancels the first, so no caller waits forever", async () => {
    let api: ReturnType<typeof useConfirm> | null = null;
    function Harness() {
      api = useConfirm();
      return <>{api.dialog}</>;
    }
    await render(<Harness />);
    let first: Promise<boolean> | null = null;
    let second: Promise<boolean> | null = null;
    await act(async () => {
      first = api!.confirm({ title: "One?", confirmLabel: "One" });
    });
    await act(async () => {
      second = api!.confirm({ title: "Two?", confirmLabel: "Two" });
    });
    expect(await first!).toBe(false);
    expect(document.querySelector('[role="alertdialog"] h2')!.textContent).toBe(
      "Two?",
    );
    await press(document.activeElement, "Escape");
    expect(await second!).toBe(false);
  });
});

describe("Sheet", () => {
  test("is a modal dialog drawn from its side, closed by Escape", async () => {
    const onClose = mock(() => undefined);
    const { host } = await render(
      <Sheet open side="left" onClose={onClose} title="Agent workbench">
        <button type="button">Inside</button>
      </Sheet>,
    );
    const sheet = host.querySelector('[role="dialog"]')!;
    expect(sheet.className).toContain("ds-sheet--left");
    expect(sheet.contains(document.activeElement)).toBe(true);
    await press(document.activeElement, "Escape");
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  test("modal={false}: no scrim, page usable, Escape only from inside, focus returns", async () => {
    function Harness() {
      const [open, setOpen] = useState(false);
      return (
        <>
          <button type="button" id="opener" onClick={() => setOpen(true)}>
            Open
          </button>
          <button type="button" id="page">
            Page
          </button>
          <Sheet
            open={open}
            modal={false}
            width={520}
            onClose={() => setOpen(false)}
            title="Agent workbench"
          >
            <button type="button" id="inside">
              Inside
            </button>
          </Sheet>
        </>
      );
    }
    const { host } = await render(<Harness />);
    const opener = host.querySelector<HTMLButtonElement>("#opener")!;
    await focus(opener);
    await click(opener);
    const sheet = host.querySelector<HTMLElement>('[role="dialog"]')!;
    expect(sheet.getAttribute("aria-modal")).toBeNull();
    expect(host.querySelector(".ds-scrim")).toBeNull();
    expect(host.querySelector(".ds-layer--nonmodal")).not.toBeNull();
    expect(sheet.style.width).toBe("520px");
    expect(sheet.contains(document.activeElement)).toBe(true);
    // Focus may go to the page and stays there (no trap).
    const page = host.querySelector<HTMLButtonElement>("#page")!;
    await focus(page);
    expect(document.activeElement).toBe(page);
    // Escape typed on the page does not close the drawer.
    await press(page, "Escape");
    expect(host.querySelector('[role="dialog"]')).not.toBeNull();
    // From inside it does, and focus goes back to the opener.
    await focus(host.querySelector("#inside"));
    await press(document.activeElement, "Escape");
    expect(host.querySelector('[role="dialog"]')).toBeNull();
    expect(document.activeElement).toBe(opener);
  });
});

describe("Menu", () => {
  const items = (log: string[]) => [
    { id: "open", label: "Open", onSelect: () => log.push("open") },
    {
      id: "archive",
      label: "Archive",
      disabled: true,
      onSelect: () => log.push("archive"),
    },
    {
      id: "delete",
      label: "Delete",
      icon: Trash2,
      tone: "danger" as const,
      onSelect: () => log.push("delete"),
    },
  ];

  test("ArrowDown opens on the first item; arrows skip disabled; Enter chooses", async () => {
    const log: string[] = [];
    const { host } = await render(<Menu label="Actions" items={items(log)} />);
    const trigger = host.querySelector("button")!;
    expect(trigger.getAttribute("aria-haspopup")).toBe("menu");
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
    await focus(trigger);
    await press(trigger, "ArrowDown");
    expect(trigger.getAttribute("aria-expanded")).toBe("true");
    const menu = host.querySelector('[role="menu"]')!;
    expect(menu.getAttribute("aria-labelledby")).toBe(trigger.id);
    expect(document.activeElement?.textContent).toBe("Open");
    await press(document.activeElement, "ArrowDown");
    expect(document.activeElement?.textContent).toBe("Delete");
    await press(document.activeElement, "ArrowDown");
    expect(document.activeElement?.textContent).toBe("Open");
    await press(document.activeElement, "End");
    await press(document.activeElement, "Enter");
    expect(log).toEqual(["delete"]);
    expect(host.querySelector('[role="menu"]')).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  test("ArrowUp opens on the last item; Escape closes and returns focus", async () => {
    const { host } = await render(<Menu label="Actions" items={items([])} />);
    const trigger = host.querySelector("button")!;
    await focus(trigger);
    await press(trigger, "ArrowUp");
    expect(document.activeElement?.textContent).toBe("Delete");
    expect(host.querySelector('[aria-disabled="true"]')!.textContent).toBe(
      "Archive",
    );
    await press(document.activeElement, "Escape");
    expect(host.querySelector('[role="menu"]')).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  test("checked items are menuitemradio with aria-checked", async () => {
    const chosen: string[] = [];
    const { host } = await render(
      <Menu
        label="Theme: System"
        iconOnly
        icon={Trash2}
        variant="ghost"
        tooltip="Theme: System"
        items={[
          {
            id: "system",
            label: "System",
            checked: true,
            onSelect: () => chosen.push("system"),
          },
          {
            id: "dark",
            label: "Dark",
            checked: false,
            onSelect: () => chosen.push("dark"),
          },
        ]}
      />,
    );
    const trigger = host.querySelector<HTMLButtonElement>(
      'button[aria-haspopup="menu"]',
    )!;
    expect(trigger.className).toContain("ds-btn--ghost");
    expect(trigger.className).toContain("ds-menu-trigger--icon");
    // The label stays the accessible name, visually hidden.
    expect(trigger.querySelector(".ds-sr-only")!.textContent).toBe(
      "Theme: System",
    );
    // An icon-only trigger has a visible tooltip with the same words.
    expect(host.querySelector('[role="tooltip"]')!.textContent).toBe(
      "Theme: System",
    );
    await focus(trigger);
    await press(trigger, "ArrowDown");
    const radios = host.querySelectorAll('[role="menuitemradio"]');
    expect(radios).toHaveLength(2);
    expect(radios[0].getAttribute("aria-checked")).toBe("true");
    expect(radios[1].getAttribute("aria-checked")).toBe("false");
    await press(document.activeElement, "ArrowDown");
    await press(document.activeElement, "Enter");
    expect(chosen).toEqual(["dark"]);
  });

  test("a badge follows the label", async () => {
    const { host } = await render(
      <Menu label="Jobs" icon={Trash2} iconOnly badge="2" items={[]} />,
    );
    expect(host.querySelector(".ds-menu-trigger__badge")!.textContent).toBe(
      "2",
    );
  });

  test("a click outside closes it", async () => {
    const { host } = await render(<Menu label="Actions" items={items([])} />);
    await click(host.querySelector("button"));
    expect(host.querySelector('[role="menu"]')).not.toBeNull();
    await fire(
      document.body,
      new PointerEvent("pointerdown", { bubbles: true }),
    );
    expect(host.querySelector('[role="menu"]')).toBeNull();
  });
});

describe("Toast", () => {
  function Trigger({
    options,
  }: {
    options: Parameters<ReturnType<typeof useToast>["show"]>[0];
  }) {
    const toast = useToast();
    return (
      <button type="button" onClick={() => toast.show(options)}>
        show
      </button>
    );
  }

  test("success goes to the polite region and hides after 4 s", async () => {
    const { host } = await render(
      <ToastProvider>
        <Trigger options={{ tone: "success", title: "Saved", duration: 40 }} />
      </ToastProvider>,
    );
    const polite = host.querySelector('[aria-live="polite"]')!;
    const urgent = host.querySelector('[aria-live="assertive"]')!;
    expect(host.querySelector("section")!.getAttribute("aria-label")).toBe(
      "Notifications",
    );
    await click(host.querySelector("button"));
    expect(polite.textContent).toContain("Saved");
    expect(urgent.textContent).toBe("");
    await flush(80);
    expect(polite.textContent).not.toContain("Saved");
    expect(TOAST_DEFAULT_MS).toBe(4000);
  });

  test("errors are assertive and stay; the close button dismisses", async () => {
    const { host } = await render(
      <ToastProvider>
        <Trigger options={{ tone: "danger", title: "Export failed" }} />
      </ToastProvider>,
    );
    await click(host.querySelector("button"));
    const urgent = host.querySelector('[aria-live="assertive"]')!;
    expect(urgent.getAttribute("role")).toBe("alert");
    expect(urgent.textContent).toContain("Export failed");
    await flush(50);
    expect(urgent.textContent).toContain("Export failed");
    await click(urgent.querySelector('[aria-label="Dismiss notification"]'));
    expect(urgent.textContent).toBe("");
  });

  test("hover pauses the timer; the action runs and dismisses", async () => {
    const onUndo = mock(() => undefined);
    const { host } = await render(
      <ToastProvider>
        <Trigger
          options={{
            title: "3 removed",
            duration: 40,
            action: { label: "Undo", onClick: onUndo },
          }}
        />
      </ToastProvider>,
    );
    await click(host.querySelector("button"));
    const card = host.querySelector(".ds-toast")!;
    await fire(card, new PointerEvent("pointerover", { bubbles: true }));
    await fire(card, new PointerEvent("pointerenter", { bubbles: false }));
    await flush(80);
    expect(host.querySelector(".ds-toast")).not.toBeNull();
    await click(host.querySelector(".ds-toast__action"));
    expect(onUndo).toHaveBeenCalledTimes(1);
    expect(host.querySelector(".ds-toast")).toBeNull();
  });

  test("at most three are visible", async () => {
    const { host } = await render(
      <ToastProvider>
        <Trigger options={{ title: "note", duration: null }} />
      </ToastProvider>,
    );
    for (let i = 0; i < 5; i += 1) await click(host.querySelector("button"));
    expect(host.querySelectorAll(".ds-toast")).toHaveLength(3);
  });

  test("useToast outside a provider is a harmless no-op", async () => {
    const { host } = await render(<Trigger options={{ title: "x" }} />);
    await click(host.querySelector("button"));
    expect(host.querySelector(".ds-toast")).toBeNull();
  });
});

describe("Modal focus trap (review fixes)", () => {
  function Trap({
    onClose,
    closeOnScrim = true,
  }: {
    onClose: () => void;
    closeOnScrim?: boolean;
  }) {
    return (
      <>
        <button type="button" id="outside">
          Outside
        </button>
        <Dialog
          open
          onClose={onClose}
          closeOnScrim={closeOnScrim}
          title="Trap"
          footer={
            <button type="button" id="inside-last">
              Save
            </button>
          }
        />
      </>
    );
  }

  test("after a press on the scrim (closeOnScrim=false), Tab and Escape still work", async () => {
    const onClose = mock(() => undefined);
    const { host } = await render(
      <Trap onClose={onClose} closeOnScrim={false} />,
    );
    const dialog = host.querySelector('[role="dialog"]')!;
    const scrim = host.querySelector(".ds-scrim")!;
    const down = new MouseEvent("mousedown", {
      bubbles: true,
      cancelable: true,
    });
    await fire(scrim, down);
    expect(down.defaultPrevented).toBe(true);
    await click(scrim);
    expect(onClose).not.toHaveBeenCalled();
    // Focus has left the panel (as after a click on a non-focusable area).
    await dropFocus();
    expect(dialog.contains(document.activeElement)).toBe(false);
    await press(document.body, "Tab");
    expect(dialog.contains(document.activeElement)).toBe(true);
    await dropFocus();
    await press(document.body, "Tab", { shiftKey: true });
    expect(document.activeElement?.id).toBe("inside-last");
    await dropFocus();
    await press(document.body, "Escape");
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  test("focus that lands outside the panel is pulled back", async () => {
    const { host } = await render(<Trap onClose={() => undefined} />);
    const dialog = host.querySelector('[role="dialog"]')!;
    await focus(host.querySelector("#outside"));
    expect(dialog.contains(document.activeElement)).toBe(true);
  });

  test("with nested dialogs only the innermost reacts to Tab and Escape", async () => {
    const outerClose = mock(() => undefined);
    function Nested() {
      const [inner, setInner] = useState(true);
      return (
        <Dialog
          open
          onClose={outerClose}
          title="Outer"
          footer={
            <button type="button" id="outer-btn">
              Outer
            </button>
          }
        >
          <Dialog
            open={inner}
            onClose={() => setInner(false)}
            title="Inner"
            footer={
              <button type="button" id="inner-btn">
                Inner
              </button>
            }
          />
        </Dialog>
      );
    }
    const { host } = await render(<Nested />);
    const dialogs = () => host.querySelectorAll('[role="dialog"]');
    expect(dialogs()).toHaveLength(2);
    const inner = dialogs()[1];
    expect(inner.contains(document.activeElement)).toBe(true);
    await focus(host.querySelector("#inner-btn"));
    await press(document.activeElement, "Tab");
    expect(inner.contains(document.activeElement)).toBe(true);
    await press(document.activeElement, "Escape");
    expect(dialogs()).toHaveLength(1);
    expect(outerClose).not.toHaveBeenCalled();
    await press(document.body, "Escape");
    expect(outerClose).toHaveBeenCalledTimes(1);
  });

  test("Escape in a menu inside a dialog closes the menu, not the dialog", async () => {
    const onClose = mock(() => undefined);
    const { host } = await render(
      <Dialog open onClose={onClose} title="With menu">
        <Menu
          label="More"
          items={[{ id: "a", label: "A", onSelect: () => undefined }]}
        />
      </Dialog>,
    );
    const trigger = host.querySelector('[aria-haspopup="menu"]')!;
    await focus(trigger);
    await press(trigger, "ArrowDown");
    expect(host.querySelector('[role="menu"]')).not.toBeNull();
    await press(document.activeElement, "Escape");
    expect(host.querySelector('[role="menu"]')).toBeNull();
    expect(onClose).not.toHaveBeenCalled();
  });
});

describe("Toast (review fixes)", () => {
  test("errors stay visible however many notes follow; notes are capped", async () => {
    let api: ReturnType<typeof useToast> | null = null;
    function Grab() {
      api = useToast();
      return null;
    }
    const { host } = await render(
      <ToastProvider>
        <Grab />
      </ToastProvider>,
    );
    await act(async () => {
      api!.show({ tone: "danger", title: "Error 1" });
      api!.show({ tone: "danger", title: "Error 2" });
      for (let i = 0; i < 5; i += 1)
        api!.show({ title: `note ${i}`, duration: null });
    });
    const urgent = host.querySelector('[aria-live="assertive"]')!;
    const polite = host.querySelector('[aria-live="polite"]')!;
    expect(urgent.querySelectorAll(".ds-toast")).toHaveLength(2);
    expect(polite.querySelectorAll(".ds-toast")).toHaveLength(
      TOAST_MAX_VISIBLE,
    );
  });

  test("a toast with an action does not hide by default", () => {
    expect(toastDuration({ title: "x" })).toBe(TOAST_DEFAULT_MS);
    expect(
      toastDuration({
        title: "x",
        action: { label: "Undo", onClick: () => undefined },
      }),
    ).toBeNull();
    expect(toastDuration({ title: "x", tone: "danger" })).toBeNull();
    expect(
      toastDuration({
        title: "x",
        duration: 50,
        action: { label: "Undo", onClick: () => undefined },
      }),
    ).toBe(50);
  });
});
