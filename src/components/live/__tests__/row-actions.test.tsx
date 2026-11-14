import { describe, expect, test } from "bun:test";
import { render, setupDom } from "@/components/ds/__tests__/dom";
import { act } from "react";
import { Breakable, RowActions } from "../live-row";
import { viewerLink } from "../management-actions";

setupDom();

describe("row actions", () => {
  test("the viewer is a primary action only when its view is ready", () => {
    expect(viewerLink("/local/x", "ready")).toEqual({
      href: "/local/x",
      external: false,
    });
    expect(viewerLink("/local/x", "pending")).toBeNull();
    expect(viewerLink("/local/x", "building")).toBeNull();
    expect(
      viewerLink(null, null, { href: "http://h/x", external: true }),
    ).toEqual({ href: "http://h/x", external: true });
    expect(viewerLink(null, "ready")).toBeNull();
  });

  test("the menu has its row's name, closes on Escape and returns focus", async () => {
    const { host } = await render(
      <RowActions name="pi05 / plates">
        <button type="button">Delete</button>
      </RowActions>,
    );
    const menu = host.querySelector("details")!;
    const summary = menu.querySelector("summary")!;
    expect(summary.getAttribute("aria-label")).toBe("Actions: pi05 / plates");
    menu.open = true;
    menu.querySelector("button")!.focus();
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    });
    expect(menu.open).toBe(false);
    expect(document.activeElement).toBe(summary);
  });

  test("a click elsewhere closes it; the session page's menu stays open", async () => {
    const { host } = await render(
      <>
        <RowActions name="a">
          <span>x</span>
        </RowActions>
        <p id="elsewhere">elsewhere</p>
      </>,
    );
    const menu = host.querySelector("details")!;
    menu.open = true;
    await act(async () => {
      host
        .querySelector("#elsewhere")!
        .dispatchEvent(new Event("pointerdown", { bubbles: true }));
    });
    expect(menu.open).toBe(false);
    const inline = await render(
      <RowActions name="b" open>
        <span>y</span>
      </RowActions>,
    );
    const kept = inline.host.querySelector("details")!;
    expect(kept.open).toBe(true);
    expect(kept.className).toContain("is-inline");
  });

  test("long identifiers may break after separators, text unchanged", async () => {
    const { host } = await render(<Breakable text="pi05_fr3 / pick-the.x" />);
    expect(host.textContent).toBe("pi05_fr3 / pick-the.x");
    expect(host.querySelectorAll("wbr").length).toBe(4);
  });
});
