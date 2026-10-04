import {
  click,
  mockMatchMedia,
  render,
  setupDom,
} from "@/components/ds/__tests__/dom";
import { describe, expect, test } from "bun:test";
import { useState } from "react";
import { InspectorLayout, InspectorPortal } from "../inspector";

setupDom();

function Form() {
  const [value, setValue] = useState("draft");
  return (
    <InspectorPortal>
      <input
        aria-label="field"
        value={value}
        onChange={(e) => setValue(e.target.value)}
      />
      <button type="button" onClick={() => setValue("changed")}>
        change
      </button>
    </InspectorPortal>
  );
}

function Page({ enabled }: { enabled: boolean }) {
  return (
    <InspectorLayout enabled={enabled}>
      <main>
        <Form />
      </main>
    </InspectorLayout>
  );
}

describe("inspector column", () => {
  test("the form renders in the right column when it is enabled", async () => {
    const restore = mockMatchMedia(["min-width: 1200px"]);
    const { host } = await render(<Page enabled />);
    restore();
    const aside = host.querySelector("aside")!;
    expect(aside.getAttribute("aria-label")).toBe("Inspector");
    expect(aside.querySelector('[aria-label="field"]')).not.toBeNull();
    expect(host.querySelector("main [aria-label='field']")).toBeNull();
  });

  test("without the column the form stays where it is", async () => {
    const { host } = await render(<Page enabled={false} />);
    expect(host.querySelector("aside")).toBeNull();
    expect(host.querySelector("main [aria-label='field']")).not.toBeNull();
  });

  test("collapsing hides the column's body but keeps the form's state", async () => {
    const restore = mockMatchMedia(["min-width: 1200px"]);
    const { host } = await render(<Page enabled />);
    restore();
    const aside = host.querySelector("aside")!;
    await click(
      [...aside.querySelectorAll("button")].find((b) =>
        b.textContent?.includes("change"),
      )!,
    );
    const toggle = aside.querySelector("[aria-expanded]")!;
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    await click(toggle);
    expect(aside.getAttribute("data-open")).toBe("false");
    expect(
      aside.querySelector(".vw-inspector-body")!.hasAttribute("hidden"),
    ).toBe(true);
    await click(aside.querySelector("[aria-expanded]"));
    expect(
      (aside.querySelector('[aria-label="field"]') as HTMLInputElement).value,
    ).toBe("changed");
  });

  test("on a narrow window the drawer starts collapsed", async () => {
    const restore = mockMatchMedia([]);
    const { host } = await render(<Page enabled />);
    restore();
    expect(host.querySelector("aside")!.getAttribute("data-open")).toBe(
      "false",
    );
  });
});
