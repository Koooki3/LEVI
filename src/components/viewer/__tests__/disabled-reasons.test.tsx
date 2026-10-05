import { click, flush, render, setupDom } from "@/components/ds/__tests__/dom";
import { describe, expect, mock, test } from "bun:test";
import type { ReactNode } from "react";
import { AuthProvider } from "@/context/auth-context";
import { TimeProvider } from "@/context/time-context";
import { LocaleProvider } from "@/components/levi-locale";
import ObjectAnnotationPanel from "@/components/object-annotation-panel";
import { PopupActions } from "../popup-actions";

setupDom();
globalThis.fetch = mock(() =>
  Promise.resolve(new Response("{}", { status: 404 })),
) as unknown as typeof fetch;

function shell(children: ReactNode) {
  return (
    <LocaleProvider>
      <AuthProvider>
        <TimeProvider duration={5}>{children}</TimeProvider>
      </AuthProvider>
    </LocaleProvider>
  );
}

/** The words a screen reader gets as the description of `button`. */
function described(host: HTMLElement, button: Element): string {
  const ids = (button.getAttribute("aria-describedby") ?? "")
    .split(" ")
    .filter(Boolean);
  return ids
    .map((id) => host.querySelector(`#${id}`)?.textContent ?? "")
    .join(" ");
}

describe("a disabled button says why, on screen", () => {
  test("Run SAM3 annotation without a worker names the missing setup", async () => {
    const { host } = await render(
      shell(
        <ObjectAnnotationPanel
          episodeId={0}
          ident={{ repoId: "local/demo" }}
          cameraKeys={["front"]}
        />,
      ),
    );
    await flush(30);
    const run = [...host.querySelectorAll("button")].find((b) =>
      b.textContent?.includes("Run SAM3 annotation"),
    )!;
    expect(run.disabled).toBe(true);
    // The reason is a visible element, and the old title-only hint is gone.
    expect(described(host, run)).toBe(
      "Complete Hub access, worker and checkpoint setup above.",
    );
    expect(run.hasAttribute("title")).toBe(false);
    expect(run.closest("[title]")).toBeNull();
    // The explanation of what Run uses is a ds tooltip (role=tooltip).
    expect(
      run.closest(".ds-tooltip-anchor")?.querySelector("[role=tooltip]")
        ?.textContent,
    ).toContain("1038lab/sam3");
  });

  test("Save preset names what is missing", async () => {
    const { host } = await render(
      shell(
        <ObjectAnnotationPanel
          episodeId={0}
          ident={{ repoId: "local/demo" }}
          cameraKeys={["front"]}
        />,
      ),
    );
    await flush(30);
    const save = [...host.querySelectorAll("button")].find(
      (b) => b.textContent === "Save preset",
    )!;
    expect(save.disabled).toBe(true);
    expect(described(host, save)).toBe("Name the preset to save it.");
  });
});

describe("quick-create popup actions", () => {
  test("Add waits for a label and says so", async () => {
    const onAdd = mock(() => undefined);
    const { host, rerender } = await render(
      <LocaleProvider>
        <PopupActions canAdd={false} onCancel={() => undefined} onAdd={onAdd} />
      </LocaleProvider>,
    );
    const add = [...host.querySelectorAll("button")].find(
      (b) => b.textContent === "Add",
    )!;
    expect(add.disabled).toBe(true);
    expect(described(host, add)).toBe("Type a label to add it.");
    await rerender(
      <LocaleProvider>
        <PopupActions canAdd onCancel={() => undefined} onAdd={onAdd} />
      </LocaleProvider>,
    );
    expect(add.disabled).toBe(false);
    expect(add.hasAttribute("aria-describedby")).toBe(false);
    expect(host.querySelector("small")).toBeNull();
    await click(add);
    expect(onAdd).toHaveBeenCalledTimes(1);
  });
});
