import { act, useState } from "react";
import { click, press, render, setupDom } from "@/components/ds/__tests__/dom";
import { afterEach, describe, expect, test } from "bun:test";
import { readFileSync } from "fs";
import { join } from "path";
import en from "@/i18n/en.json";
import { errorOf } from "../deploy-api";
import { ConfirmRefusal, ConfirmWindow } from "../confirm-window";
import {
  button,
  doubleClick,
  textFields,
  tick,
  tickAll,
} from "./actions-helpers";

setupDom();
afterEach(() => {
  try {
    localStorage.clear();
  } catch {
    // no storage in this DOM
  }
});

const dialog = () => document.querySelector("[role=dialog]") as HTMLElement;

/** A host that opens the window from a button and counts what it sends. */
function Host({
  send = () => undefined,
  attest = [{ code: "hands", label: "Hands are clear" }],
  ready = true,
  working = false,
  refused,
}: {
  send?: (ticked: Record<string, boolean>) => unknown;
  attest?: Array<{ code: string; label: string }>;
  ready?: boolean;
  working?: boolean;
  refused?: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button onClick={() => setOpen(true)}>Open</button>
      <ConfirmWindow
        open={open}
        title="Do the thing"
        intro="What it does."
        estop="Keep a hand on the physical e-stop."
        hazards={["It moves.", "It is slow."]}
        notes={["Gates stop mistakes."]}
        attest={attest}
        ready={ready}
        working={working}
        refused={refused}
        confirmLabel="Do it"
        onCancel={() => setOpen(false)}
        onConfirm={(ticked) => {
          const sent = send(ticked);
          return sent;
        }}
      />
    </>
  );
}

describe("the confirm window", () => {
  test("a title, the hazards and the ticks; Cancel has the focus; Confirm waits for every tick", async () => {
    await render(
      <Host
        attest={[
          { code: "a", label: "First check" },
          { code: "b", label: "Second check" },
        ]}
      />,
    );
    await click(button(document.body, "Open")!);
    const d = dialog();
    expect(d.textContent).toContain("Do the thing");
    expect(d.textContent).toContain("What it does.");
    expect(d.textContent).toContain("Keep a hand on the physical e-stop.");
    expect(
      Array.from(d.querySelectorAll("li")).map((li) => li.textContent),
    ).toEqual(["It moves.", "It is slow."]);
    expect(d.textContent).toContain("Gates stop mistakes.");
    expect(textFields(d)).toHaveLength(0);
    expect(document.activeElement).toBe(button(d, "Cancel"));
    expect(button(d, "Do it")!.disabled).toBe(true);
    await click(d.querySelectorAll("input[type=checkbox]")[0]);
    expect(button(d, "Do it")!.disabled).toBe(true);
    await click(d.querySelectorAll("input[type=checkbox]")[1]);
    expect(button(d, "Do it")!.disabled).toBe(false);
    // Un-ticking one switches it off again.
    await click(d.querySelectorAll("input[type=checkbox]")[0]);
    expect(button(d, "Do it")!.disabled).toBe(true);
  });

  test("the footer says how many checks are still to tick, so a window taller than the screen explains its off button", async () => {
    await render(
      <Host
        attest={[
          { code: "a", label: "First check" },
          { code: "b", label: "Second check" },
          { code: "c", label: "Third check" },
        ]}
      />,
    );
    await click(button(document.body, "Open")!);
    const d = dialog();
    const left = () =>
      d.querySelector("[data-testid=marvel0-confirm-ticks-left]");
    // It sits in the footer, beside the buttons, not in the scrolling text.
    expect(left()!.closest(".ds-dialog__footer")).not.toBeNull();
    expect(left()!.textContent).toBe("3 of 3 checks still to tick.");
    await click(d.querySelectorAll("input[type=checkbox]")[0]);
    expect(left()!.textContent).toBe("2 of 3 checks still to tick.");
    await tickAll(d);
    expect(left()).toBeNull();
    expect(button(d, "Do it")!.disabled).toBe(false);
    // A window with no tick has nothing to say.
    await click(button(d, "Cancel")!);
    await render(<Host attest={[]} />);
    const opens = Array.from(document.body.querySelectorAll("button")).filter(
      (b) => b.textContent === "Open",
    );
    await click(opens[opens.length - 1]);
    const windows = document.querySelectorAll("[role=dialog]");
    expect(
      (windows[windows.length - 1] as HTMLElement).querySelector(
        "[data-testid=marvel0-confirm-ticks-left]",
      ),
    ).toBeNull();
  });

  test("the refusal of the write is shown outside the scrolling text, so a tall window cannot hide it at its foot", async () => {
    await render(<Host refused={<p data-testid="refusal">Refused.</p>} />);
    await click(button(document.body, "Open")!);
    const d = dialog();
    const refusal = d.querySelector("[data-testid=refusal]")!;
    expect(refusal).not.toBeNull();
    expect(
      d.querySelector("[data-testid=marvel0-confirm]")!.contains(refusal),
    ).toBe(false);
  });

  test("the text between the title and the buttons is one scrolling group a keyboard can reach, and the panel does not scroll as a whole", async () => {
    await render(<Host />);
    await click(button(document.body, "Open")!);
    const d = dialog();
    expect(d.className).toContain("rg-confirm");
    const text = d.querySelector("[data-testid=marvel0-confirm]")!;
    expect(text.getAttribute("tabindex")).toBe("0");
    expect(text.getAttribute("role")).toBe("group");
    expect(text.getAttribute("aria-label")).toBe("Do the thing");
    // Cancel keeps the initial focus (a stray Enter cancels).
    expect(document.activeElement).toBe(button(d, "Cancel"));
  });

  test("a window without ticks is on at once, but the caller's own condition can keep it off", async () => {
    await render(<Host attest={[]} ready={false} />);
    await click(button(document.body, "Open")!);
    expect(button(dialog(), "Do it")!.disabled).toBe(true);
    await render(<Host attest={[]} />);
    const opens = Array.from(document.body.querySelectorAll("button")).filter(
      (b) => b.textContent === "Open",
    );
    await click(opens[opens.length - 1]);
    const windows = document.querySelectorAll("[role=dialog]");
    expect(
      button(windows[windows.length - 1] as HTMLElement, "Do it")!.disabled,
    ).toBe(false);
  });

  test("the ticks that were set do not outlive the opening", async () => {
    await render(<Host />);
    await click(button(document.body, "Open")!);
    await tickAll(dialog());
    expect(button(dialog(), "Do it")!.disabled).toBe(false);
    await click(button(dialog(), "Cancel")!);
    await click(button(document.body, "Open")!);
    expect(
      (dialog().querySelector("input[type=checkbox]") as HTMLInputElement)
        .checked,
    ).toBe(false);
    expect(button(dialog(), "Do it")!.disabled).toBe(true);
  });

  test("Cancel, Escape and the backdrop cancel and send nothing", async () => {
    const sent: unknown[] = [];
    await render(<Host send={(t) => sent.push(t)} />);
    await click(button(document.body, "Open")!);
    await tickAll(dialog());
    await click(button(dialog(), "Cancel")!);
    expect(document.querySelector("[role=dialog]")).toBeNull();
    await click(button(document.body, "Open")!);
    await press(document.activeElement, "Escape");
    expect(document.querySelector("[role=dialog]")).toBeNull();
    await click(button(document.body, "Open")!);
    await click(document.querySelector(".ds-scrim"));
    expect(document.querySelector("[role=dialog]")).toBeNull();
    expect(sent).toEqual([]);
  });

  test("a double click sends once; a second send is possible only after the first ended", async () => {
    let release: () => void = () => {};
    const sent: Array<Record<string, boolean>> = [];
    await render(
      <Host
        send={(ticked) => {
          sent.push(ticked);
          return new Promise<void>((resolve) => {
            release = resolve;
          });
        }}
      />,
    );
    await click(button(document.body, "Open")!);
    await tickAll(dialog());
    await doubleClick(button(dialog(), "Do it"));
    expect(sent).toEqual([{ hands: true }]);
    // Still on its way: another click is ignored.
    await click(button(dialog(), "Do it")!);
    expect(sent).toHaveLength(1);
    await act(async () => release());
    await tick(2, 5);
    await click(button(dialog(), "Do it")!);
    expect(sent).toHaveLength(2);
  });

  test("while the write is on its way both buttons wait and Escape does not cancel", async () => {
    await render(<Host working />);
    await click(button(document.body, "Open")!);
    await tickAll(dialog());
    expect(button(dialog(), "Cancel")!.disabled).toBe(true);
    expect(button(dialog(), "Do it")!.disabled).toBe(true);
    expect(button(dialog(), "Do it")!.getAttribute("aria-busy")).toBe("true");
    await press(dialog(), "Escape");
    expect(document.querySelector("[role=dialog]")).not.toBeNull();
  });

  test("the refusal is shown inside the window", async () => {
    await render(
      <Host
        refused={
          <ConfirmRefusal
            failure={errorOf(409, { detail: { code: "busy", message: "m" } })}
          >
            Busy now.
          </ConfirmRefusal>
        }
      />,
    );
    await click(button(document.body, "Open")!);
    const shown = dialog().querySelector(
      "[data-testid=marvel0-confirm-refused]",
    )!;
    expect(shown.textContent).toBe("Busy now.");
    expect(shown.getAttribute("role")).toBe("alert");
    expect(shown.getAttribute("data-internal")).toBeNull();
  });
});

describe("a refused phrase is the page's own error", () => {
  test("it is said as an internal error with the phrase the server holds, and never as something to type", async () => {
    await render(
      <ConfirmRefusal
        failure={errorOf(400, {
          detail: {
            code: "confirm_required",
            message: "Type the confirmation phrase exactly: stop it",
            phrase: "stop it",
          },
        })}
      >
        Type the confirmation phrase exactly: stop it
      </ConfirmRefusal>,
    );
    const shown = document.querySelector(
      "[data-testid=marvel0-confirm-refused]",
    )!;
    expect(shown.getAttribute("data-internal")).toBe("true");
    expect(shown.textContent).toContain(en["marvel0.ctl.confirm.internal"]);
    expect(shown.textContent).toContain("The server expects: stop it");
    expect(shown.textContent).not.toContain("Type the confirmation phrase");
  });

  test("without a phrase from the server there is still no prompt", async () => {
    await render(
      <ConfirmRefusal
        failure={errorOf(400, {
          detail: { code: "confirm_required", message: "Type it" },
        })}
      >
        Type it
      </ConfirmRefusal>,
    );
    const text = document.querySelector(
      "[data-testid=marvel0-confirm-refused]",
    )!.textContent!;
    expect(text).toBe(en["marvel0.ctl.confirm.internal"]);
  });
});

describe("no start or stop asks for a typed phrase", () => {
  // The two texts a person still types cut something in use off: the
  // override of a stop of C with a client connected (stack-dialog.tsx) and
  // the extra phrase of a stop of the policy server under a client
  // (deploy-panel.tsx). Everything else is a window of ticks and a click.
  const dir = join(import.meta.dir, "..");
  const source = (file: string) => readFileSync(join(dir, file), "utf8");

  test("the shared window, the run card and the hosted client's start have no text field at all", () => {
    for (const file of [
      "confirm-window.tsx",
      "run-panel.tsx",
      "run-start.tsx",
      "stack-panel.tsx",
      "stack-sequence.tsx",
      "stack-controls.tsx",
    ])
      expect([
        file,
        /<Input\b|<textarea|type="text"/.test(source(file)),
      ]).toEqual([file, false]);
  });

  test("the stack window has one field, the override, and the policy server card has the scale and the extra phrase", () => {
    expect(source("stack-dialog.tsx").match(/<Input\b/g)).toHaveLength(1);
    expect(source("stack-dialog.tsx")).toContain("spec.override");
    // The pause of the whole stack's start is a switch (a choice), not a field.
    expect(source("stack-dialog.tsx").match(/<Switch\b/g)).toHaveLength(1);
    // The scale of the guidance and the extra phrase.
    expect(source("deploy-panel.tsx").match(/<Input\b/g)).toHaveLength(2);
  });

  test("the hint 'Type exactly: {phrase}' is used only for those two overrides", () => {
    const users = [
      "stack-dialog.tsx",
      "deploy-panel.tsx",
      "run-panel.tsx",
      "run-start.tsx",
      "confirm-window.tsx",
    ].filter((file) => /phrase_hint/.test(source(file)));
    expect(users.sort()).toEqual(["deploy-panel.tsx", "stack-dialog.tsx"]);
    for (const file of users)
      expect([file, source(file).match(/phrase_hint/g)]).toEqual([
        file,
        ["phrase_hint"],
      ]);
  });

  test("the catalogue has no sentence that asks for the phrase of an action to be typed, except the two overrides'", () => {
    const E = en as Record<string, string>;
    const asks = Object.entries(E).filter(
      ([key, value]) =>
        /^marvel0\.(ctl|run|deploy|stack|err|cmd|scope|chain|act|step)\./.test(
          key,
        ) &&
        /\btyped?\b[^.]*\bphrase\b|must be typed|Type exactly|\btype it\b/i.test(
          value,
        ),
    );
    expect(asks.map(([key]) => key).sort()).toEqual([
      "marvel0.ctl.block.client_connected", // stopping C: "... or type the override phrase"
      "marvel0.ctl.dialog.client_on_server",
      "marvel0.ctl.dialog.phrase_hint", // the hint under the override field
      "marvel0.deploy.dialog.phrase_hint", // the hint under the extra phrase
      "marvel0.err.client_connected_stop", // stopping the policy server under a client
    ]);
  });
});
