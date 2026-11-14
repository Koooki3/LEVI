import { render, setupDom } from "@/components/ds/__tests__/dom";
import { fire } from "@/components/ds/__tests__/dom";
import { describe, expect, test } from "bun:test";
import { RunTimeline } from "../run-timeline";
import type { RunEvent } from "../types";

setupDom();

const event = (seq: number, at: number): RunEvent => ({
  seq,
  at,
  kind: "note",
  from: null,
  to: null,
  reason: null,
});
const day = 86_400_000;
const base = Date.UTC(2026, 10, 14, 12, 0, 0);

function geometry(node: HTMLElement, height: number, view = 100) {
  Object.defineProperty(node, "scrollHeight", {
    value: height,
    configurable: true,
  });
  Object.defineProperty(node, "clientHeight", {
    value: view,
    configurable: true,
  });
}

describe("the event timeline's scrolling", () => {
  test("is a keyboard-reachable named region", async () => {
    const { host } = await render(
      <RunTimeline events={[event(1, base)]} blind={false} />,
    );
    const list = host.querySelector("ol")!;
    expect(list.getAttribute("tabindex")).toBe("0");
    expect(list.getAttribute("aria-label")).toBe("Events and evidence");
  });

  test("follows the newest event while the reader is at the end", async () => {
    const { host, rerender } = await render(
      <RunTimeline events={[event(1, base)]} blind={false} />,
    );
    const list = host.querySelector("ol")!;
    geometry(list, 500);
    await rerender(
      <RunTimeline
        events={[event(1, base), event(2, base + 1)]}
        blind={false}
      />,
    );
    expect(list.scrollTop).toBe(500);
  });

  test("leaves the scroll alone once the reader scrolled up", async () => {
    const { host, rerender } = await render(
      <RunTimeline events={[event(1, base)]} blind={false} />,
    );
    const list = host.querySelector("ol")!;
    geometry(list, 500);
    list.scrollTop = 40;
    await fire(list, new Event("scroll"));
    await rerender(
      <RunTimeline
        events={[event(1, base), event(2, base + 1)]}
        blind={false}
      />,
    );
    expect(list.scrollTop).toBe(40);
    // Back at the end: following resumes.
    list.scrollTop = 400;
    await fire(list, new Event("scroll"));
    await rerender(
      <RunTimeline
        events={[event(1, base), event(2, base + 1), event(3, base + 2)]}
        blind={false}
      />,
    );
    expect(list.scrollTop).toBe(500);
  });
});

describe("the event timeline's times", () => {
  test("events on one day show only the time", async () => {
    const { host } = await render(
      <RunTimeline
        events={[event(1, base), event(2, base + 60_000)]}
        blind={false}
      />,
    );
    for (const time of Array.from(host.querySelectorAll("time")))
      expect(time.textContent).not.toMatch(/2026|11\/14|14\/11/);
  });
  test("events that span days show the date too", async () => {
    const { host } = await render(
      <RunTimeline
        events={[event(1, base), event(2, base + 2 * day)]}
        blind={false}
      />,
    );
    const texts = Array.from(host.querySelectorAll("time")).map(
      (n) => n.textContent!,
    );
    expect(texts[0]).toMatch(/11\/1[34]\/2026|2026/);
    expect(texts[0]).not.toBe(texts[1]);
    expect(texts[1]).toMatch(/2026/);
  });
});
