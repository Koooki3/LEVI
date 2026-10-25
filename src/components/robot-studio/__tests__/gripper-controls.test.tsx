import { act } from "react";
import { describe, expect, test } from "bun:test";
import {
  click,
  render,
  setupDom,
  waitFor,
} from "@/components/ds/__tests__/dom";
import { createStackApi, type StackApi } from "@/components/marvel0/stack-api";
import GripperControls, { parseGripperStack } from "../gripper-controls";

setupDom();
const documentOf = () => ({
  schema: "levi.marvel0.stack.v1",
  profile: "real",
  sampled_at: Date.now() / 1000,
  act: {
    enabled: true,
    gripper_test: { state: "idle", can_run: true, blockers: [], last: null },
  },
});
function fixture() {
  const posts: unknown[] = [];
  const api: StackApi = {
    ...createStackApi(),
    read: async () => ({ ok: true, status: 200, data: documentOf() }),
    gripper: async (body) => {
      posts.push(body);
      return { ok: true, status: 200, data: {} };
    },
  };
  return { api, posts };
}
const byId = <T extends HTMLElement>(host: HTMLElement, name: string) =>
  host.querySelector<T>(`[data-testid="studio-gripper-${name}"]`)!;
async function arm(host: HTMLElement) {
  await waitFor(() => !byId<HTMLButtonElement>(host, "open").disabled);
}
async function event(
  node: HTMLElement,
  name: string,
  properties: Record<string, unknown> = {},
) {
  await act(async () => {
    if (name === "keydown" || name === "keyup") node.focus();
    const e =
      name === "keydown" || name === "keyup"
        ? new KeyboardEvent(name, { bubbles: true })
        : new Event(name, { bubbles: true });
    for (const [k, v] of Object.entries(properties))
      Object.defineProperty(e, k, { value: v });
    node.dispatchEvent(e);
  });
}
async function change(input: HTMLInputElement, value: string) {
  await act(async () => {
    input.focus();
    Object.getOwnPropertyDescriptor(
      HTMLInputElement.prototype,
      "value",
    )!.set!.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
    input.dispatchEvent(
      new KeyboardEvent("keyup", { key: "e", bubbles: true }),
    );
  });
}

describe("independent finite gripper commands", () => {
  test("rejects stale and malformed gate payloads", () => {
    expect(parseGripperStack(documentOf())).not.toBeNull();
    expect(parseGripperStack({ ...documentOf(), sampled_at: 1 })).toBeNull();
    for (const sampled_at of [Infinity, NaN, -1])
      expect(parseGripperStack({ ...documentOf(), sampled_at })).toBeNull();
    const doc = documentOf();
    expect(
      parseGripperStack({
        ...doc,
        act: { ...doc.act, gripper_test: { can_run: true } },
      }),
    ).toBeNull();
  });
  test("mount and missing feedback never move; measured feedback remains distinct", async () => {
    const f = fixture();
    const view = await render(
      <GripperControls api={f.api} measuredOpeningRatio={null} />,
    );
    expect(byId<HTMLButtonElement>(view.host, "open").disabled).toBe(true);
    expect(f.posts).toEqual([]);
    await view.rerender(<></>);
  });
  test("one click sends one finite command without a translation session", async () => {
    const f = fixture();
    const view = await render(
      <GripperControls api={f.api} measuredOpeningRatio={0.4} />,
    );
    await arm(view.host);
    expect(view.host.querySelector('input[type="checkbox"]')).toBeNull();
    await click(byId(view.host, "open"));
    expect(f.posts).toHaveLength(1);
    expect(f.posts[0]).toMatchObject({
      action: "open",
      confirm: "gripper open",
      attest: { hands_clear: true },
    });
    await view.rerender(<></>);
  });
  test("slider change does not dispatch until pointer release, and cancel never dispatches", async () => {
    const f = fixture();
    const view = await render(
      <GripperControls api={f.api} measuredOpeningRatio={0.4} />,
    );
    await arm(view.host);
    const input = byId<HTMLInputElement>(view.host, "target");
    await event(input, "pointerdown", { button: 0, pointerId: 1 });
    await change(input, "0.7");
    expect(f.posts).toHaveLength(0);
    expect(byId(view.host, "measured").textContent).toBe("40%");
    await event(input, "pointerup", { pointerId: 1 });
    await event(input, "pointerup", { pointerId: 1 });
    expect(f.posts).toHaveLength(1);
    expect(f.posts[0]).toMatchObject({
      action: "position",
      confirm: "gripper position",
      opening_ratio: 0.7,
    });
    await arm(view.host);
    await event(input, "pointerdown", { button: 0, pointerId: 2 });
    await change(input, "0.8");
    await event(input, "pointercancel", { pointerId: 2 });
    expect(input.value).toBe("0.4");
    expect(byId(view.host, "target-value").textContent).toBe("40%");
    await event(input, "pointerup", { pointerId: 2 });
    expect(f.posts).toHaveLength(1);
    await view.rerender(<></>);
  });
  test("synchronous clicks deduplicate, and network uncertainty cannot retry", async () => {
    const f = fixture();
    let finish!: (answer: Awaited<ReturnType<StackApi["gripper"]>>) => void;
    f.api.gripper = (body) => {
      f.posts.push(body);
      return new Promise((resolve) => {
        finish = resolve;
      });
    };
    const view = await render(
      <GripperControls api={f.api} measuredOpeningRatio={0.4} />,
    );
    await arm(view.host);
    await act(async () => {
      byId(view.host, "open").click();
      byId(view.host, "close").click();
    });
    expect(f.posts).toHaveLength(1);
    await act(async () => {
      finish({
        ok: false,
        status: 0,
        code: "network",
        message: "unknown",
        extra: {},
        field: null,
        fields: [],
      });
    });
    await waitFor(() => !view.host.querySelector('[aria-busy="true"]'));
    expect(byId<HTMLButtonElement>(view.host, "open").disabled).toBe(true);
    expect(view.host.querySelector('[role="alert"]')).not.toBeNull();
    expect(f.posts).toHaveLength(1);
    await view.rerender(<></>);
  });
  test("a valid keyboard release commits once; repeated release and blur do not commit", async () => {
    const f = fixture();
    const view = await render(
      <GripperControls api={f.api} measuredOpeningRatio={0.4} />,
    );
    await arm(view.host);
    const input = byId<HTMLInputElement>(view.host, "target");
    await event(input, "keydown", { key: "ArrowRight", repeat: false });
    await change(input, "0.5");
    await event(input, "keyup", { key: "ArrowRight", repeat: true });
    expect(f.posts).toHaveLength(0);
    await event(input, "keyup", { key: "ArrowRight", repeat: false });
    await event(input, "keyup", { key: "ArrowRight", repeat: false });
    expect(f.posts).toHaveLength(1);
    await arm(view.host);
    await event(input, "keydown", { key: "ArrowRight", repeat: false });
    await change(input, "0.6");
    await event(input, "focusout");
    expect(input.value).toBe("0.4");
    await event(input, "keyup", { key: "ArrowRight", repeat: false });
    expect(f.posts).toHaveLength(1);
    await view.rerender(<></>);
  });
  test("requested target survives lost capture and new measured feedback", async () => {
    const f = fixture();
    const view = await render(
      <GripperControls api={f.api} measuredOpeningRatio={0.4} />,
    );
    await arm(view.host);
    const input = byId<HTMLInputElement>(view.host, "target");
    await event(input, "pointerdown", { button: 0, pointerId: 5 });
    await change(input, "0.7");
    await event(input, "pointerup", { pointerId: 5 });
    await event(input, "lostpointercapture", { pointerId: 5 });
    await view.rerender(
      <GripperControls api={f.api} measuredOpeningRatio={0.6} />,
    );
    expect(byId(view.host, "measured").textContent).toBe("60%");
    expect(byId(view.host, "target-value").textContent).toBe("70%");
    await arm(view.host);
    await click(byId(view.host, "close"));
    expect(byId(view.host, "target-value").textContent).toBe("0%");
    await view.rerender(
      <GripperControls api={f.api} measuredOpeningRatio={0.2} />,
    );
    expect(byId(view.host, "target-value").textContent).toBe("0%");
    await view.rerender(<></>);
  });
  test("lost feedback cancels a gesture and restored feedback cannot submit it", async () => {
    const f = fixture();
    const view = await render(
      <GripperControls api={f.api} measuredOpeningRatio={0.4} />,
    );
    await arm(view.host);
    const input = byId<HTMLInputElement>(view.host, "target");
    await event(input, "pointerdown", { button: 0, pointerId: 6 });
    await change(input, "0.7");
    await view.rerender(
      <GripperControls api={f.api} measuredOpeningRatio={null} />,
    );
    await view.rerender(
      <GripperControls api={f.api} measuredOpeningRatio={0.5} />,
    );
    await event(input, "pointerup", { pointerId: 6 });
    expect(f.posts).toHaveLength(0);
    expect(byId(view.host, "target-value").textContent).toBe("50%");
    await view.rerender(<></>);
  });
  test("Escape and unrelated keyup cannot commit a keyboard target", async () => {
    const f = fixture();
    const view = await render(
      <GripperControls api={f.api} measuredOpeningRatio={0.4} />,
    );
    await arm(view.host);
    const input = byId<HTMLInputElement>(view.host, "target");
    await event(input, "keydown", { key: "ArrowRight", repeat: false });
    await change(input, "0.5");
    await event(input, "keyup", { key: "a", repeat: false });
    expect(f.posts).toHaveLength(0);
    await event(input, "keydown", { key: "Escape", repeat: false });
    await event(input, "keyup", { key: "ArrowRight", repeat: false });
    expect(f.posts).toHaveLength(0);
    await view.rerender(<></>);
  });
});
