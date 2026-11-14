import {
  click,
  flush,
  render,
  setupDom,
  waitFor,
} from "@/components/ds/__tests__/dom";
import { afterEach, describe, expect, test } from "bun:test";
import { mockFetch, question } from "./fixtures";
import { SceneCheck } from "../scene-check";

setupDom();
let restore = () => {};
afterEach(() => restore());

const send = (host: HTMLElement) =>
  Array.from(host.querySelectorAll("button")).find(
    (b) => b.textContent?.trim() === "Send answers",
  ) as HTMLButtonElement;
const radio = (host: HTMLElement, legend: string, label: string) => {
  const set = Array.from(host.querySelectorAll("fieldset")).find((f) =>
    f.textContent?.includes(legend),
  )!;
  return Array.from(set.querySelectorAll("label"))
    .find((l) => l.textContent?.trim() === label)!
    .querySelector("input")!;
};
const reasonOf = (host: HTMLElement) => {
  const id = send(host).getAttribute("aria-describedby");
  return id ? host.querySelector(`[id="${id}"]`) : null;
};

describe("the scene check's Send button", () => {
  test("says how many required conditions are still open, tied to the button", async () => {
    const { host } = await render(
      <SceneCheck runId="run-1" question={question()} />,
    );
    expect(send(host).disabled).toBe(true);
    expect(reasonOf(host)?.textContent).toBe(
      "Answer 2 more required conditions",
    );
    await click(radio(host, "the plate is on the rack", "True"));
    expect(reasonOf(host)?.textContent).toBe(
      "Answer 1 more required condition",
    );
    await click(radio(host, "the cup is upright", "Cannot tell"));
    expect(send(host).disabled).toBe(false);
    expect(reasonOf(host)).toBeNull();
    expect(host.textContent).not.toContain("more required condition");
  });

  test("an optional condition does not count", async () => {
    const { host } = await render(
      <SceneCheck runId="run-1" question={question()} />,
    );
    await click(radio(host, "the plate is on the rack", "True"));
    await click(radio(host, "the cup is upright", "False"));
    expect(host.textContent).not.toContain("more required condition");
  });

  test("while sending it is loading, not natively disabled", async () => {
    let release: (v: unknown) => void = () => {};
    const gate = new Promise((resolve) => (release = resolve));
    const m = mockFetch({ "POST runs/run-1/scene-answer": () => gate });
    restore = m.restore;
    const { host } = await render(
      <SceneCheck runId="run-1" question={question()} />,
    );
    await click(radio(host, "the plate is on the rack", "True"));
    await click(radio(host, "the cup is upright", "True"));
    await click(send(host));
    expect(send(host).getAttribute("aria-busy")).toBe("true");
    expect(send(host).hasAttribute("disabled")).toBe(false);
    await click(send(host));
    expect(
      m.calls.filter((c) => c.path === "runs/run-1/scene-answer").length,
    ).toBe(1);
    release({});
    await waitFor(() => host.textContent?.includes("Answers sent"));
    await flush(10);
  });

  test("a terminal-only question still points at the terminal note", async () => {
    const { host } = await render(
      <SceneCheck runId="run-1" question={question()} terminalOnly />,
    );
    const id = send(host).getAttribute("aria-describedby")!;
    expect(host.querySelector(`[id="${id}"]`)?.textContent).toContain(
      "terminal",
    );
  });
});
