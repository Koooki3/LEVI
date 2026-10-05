import { press, render, setupDom } from "@/components/ds/__tests__/dom";
import { describe, expect, mock, test } from "bun:test";
import { act } from "react";
import { AtomEditor } from "@/components/annotations-panel";
import { LocaleProvider } from "@/components/levi-locale";
import { AnnotationsProvider } from "@/context/annotations-context";
import { TimeProvider } from "@/context/time-context";
import type { LanguageAtom } from "@/types/language.types";

setupDom();
globalThis.fetch = mock(() =>
  Promise.resolve(new Response("{}", { status: 404 })),
) as unknown as typeof fetch;

const base = {
  role: "assistant",
  content: "grasp",
  style: "subtask",
  timestamp: 1.23456,
  camera: null,
} as unknown as LanguageAtom;

function editor(
  onChange: (u: Partial<LanguageAtom>) => void,
  atom: LanguageAtom = base,
) {
  return (
    <LocaleProvider>
      <TimeProvider duration={10}>
        <AnnotationsProvider>
          <AtomEditor
            atom={atom}
            cameraKeys={[]}
            vocabulary={{ subtasks: [] } as never}
            onChange={onChange}
            onDelete={() => undefined}
          />
        </AnnotationsProvider>
      </TimeProvider>
    </LocaleProvider>
  );
}

function field(host: HTMLElement): HTMLInputElement {
  return host.querySelector<HTMLInputElement>(".ts-row input")!;
}

async function type(input: HTMLInputElement, value: string) {
  await act(async () => {
    // The prototype setter, so React's value tracker still sees a change.
    Object.getOwnPropertyDescriptor(
      HTMLInputElement.prototype,
      "value",
    )!.set!.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    // React DOM under happy-dom takes its change events from key events.
    input.dispatchEvent(
      new KeyboardEvent("keyup", { bubbles: true, key: "e" }),
    );
  });
}

describe("timestamp field shows two decimals but never rewrites the stored time", () => {
  test("the display is rounded", async () => {
    const { host } = await render(editor(() => undefined));
    expect(field(host).value).toBe("1.23");
  });

  test("Enter or blur on an untouched field writes nothing", async () => {
    const onChange = mock((u: Partial<LanguageAtom>) => void u);
    const { host } = await render(editor(onChange));
    const input = field(host);
    await act(async () => input.focus());
    await press(input, "Enter");
    await act(async () => input.blur());
    expect(onChange).not.toHaveBeenCalled();
    expect(input.value).toBe("1.23");
  });

  test("a changed value is written exactly", async () => {
    const onChange = mock((u: Partial<LanguageAtom>) => void u);
    const { host } = await render(editor(onChange));
    const input = field(host);
    await act(async () => input.focus());
    await type(input, "1.3");
    await press(input, "Enter");
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith({ timestamp: 1.3 });
  });

  test("Escape restores the rounded display", async () => {
    const onChange = mock((u: Partial<LanguageAtom>) => void u);
    const { host } = await render(editor(onChange));
    const input = field(host);
    await act(async () => input.focus());
    await type(input, "9.9");
    expect(input.value).toBe("9.9");
    await press(input, "Escape");
    expect(input.value).toBe("1.23");
    expect(onChange).not.toHaveBeenCalled();
  });

  test("snap to frame from an untouched field snaps the stored time, not the rounded one", async () => {
    const onChange = mock((u: Partial<LanguageAtom>) => void u);
    const { host } = await render(editor(onChange));
    const snapButton = [...host.querySelectorAll("button")].find(
      (b) => b.textContent === "snap to frame",
    )!;
    // Without frame timestamps snap() is the identity: the exact value
    // must be passed through (1.23456), not "1.23".
    await act(async () => {
      snapButton.dispatchEvent(
        new Event("pointerdown", { bubbles: true, cancelable: true }),
      );
    });
    expect(onChange).toHaveBeenCalledWith({ timestamp: 1.23456 });
  });
});
