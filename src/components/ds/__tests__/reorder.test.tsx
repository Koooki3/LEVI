import { setupDom, focus, mockMatchMedia, press, render } from "./dom";
import { describe, expect, test } from "bun:test";
import { useState } from "react";
import {
  DS_SPRING,
  ReorderList,
  dsLayoutTransition,
  moveItem,
} from "../ReorderList";

setupDom();

describe("moveItem", () => {
  test("moves without mutating and clamps the target", () => {
    const list = ["a", "b", "c", "d"];
    expect(moveItem(list, 0, 2)).toEqual(["b", "c", "a", "d"]);
    expect(moveItem(list, 3, 0)).toEqual(["d", "a", "b", "c"]);
    expect(moveItem(list, 1, 99)).toEqual(["a", "c", "d", "b"]);
    expect(moveItem(list, 9, 0)).toEqual(list);
    expect(list).toEqual(["a", "b", "c", "d"]);
  });
});

describe("dsLayoutTransition", () => {
  test("springs normally and snaps under reduced motion", () => {
    expect(dsLayoutTransition(false)).toBe(DS_SPRING);
    expect(dsLayoutTransition(true)).toEqual({ duration: 0 });
  });
});

function Harness() {
  const [items, setItems] = useState(["plates", "screws", "cups"]);
  return (
    <ReorderList
      label="Sources"
      items={items}
      onReorder={setItems}
      getKey={(item) => item}
      getLabel={(item) => item}
      renderItem={(item) => <span className="text">{item}</span>}
    />
  );
}

const order = (host: HTMLElement) =>
  Array.from(host.querySelectorAll(".text")).map((n) => n.textContent);

describe("ReorderList", () => {
  test("a labelled list; each handle names its item and the keys", async () => {
    const { host } = await render(<Harness />);
    const list = host.querySelector("ul")!;
    expect(list.getAttribute("aria-label")).toBe("Sources");
    const handles = host.querySelectorAll(".ds-reorder__handle");
    expect(handles[1].getAttribute("aria-label")).toBe("Move screws");
    const help = host.querySelector(
      `#${CSS.escape(handles[1].getAttribute("aria-describedby")!)}`,
    );
    expect(help!.textContent).toContain("up and down arrows");
  });

  test("arrow keys on the handle move the item, keep focus and announce", async () => {
    const { host } = await render(<Harness />);
    const handle = () =>
      Array.from(host.querySelectorAll(".ds-reorder__handle")).find(
        (h) => h.getAttribute("aria-label") === "Move screws",
      )!;
    await focus(handle());
    await press(handle(), "ArrowUp");
    expect(order(host)).toEqual(["screws", "plates", "cups"]);
    expect(document.activeElement).toBe(handle());
    expect(host.querySelector('[role="status"]')!.textContent).toBe(
      "Moved screws to position 1 of 3",
    );
    await press(handle(), "ArrowUp");
    expect(order(host)).toEqual(["screws", "plates", "cups"]);
    await press(handle(), "End");
    expect(order(host)).toEqual(["plates", "cups", "screws"]);
  });

  test("renders and reorders the same under reduced motion", async () => {
    const restore = mockMatchMedia(["prefers-reduced-motion"]);
    try {
      const { host } = await render(<Harness />);
      const first = host.querySelector(".ds-reorder__handle")!;
      await focus(first);
      await press(first, "ArrowDown");
      expect(order(host)).toEqual(["screws", "plates", "cups"]);
      // No drag scale is set up when motion is reduced.
      expect(host.innerHTML).not.toContain("scale(1.02)");
    } finally {
      restore();
    }
  });
});
