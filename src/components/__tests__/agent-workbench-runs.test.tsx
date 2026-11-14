import { act } from "react";
import { flush, render, setupDom, waitFor } from "../ds/__tests__/dom";
import { afterEach, describe, expect, mock, test } from "bun:test";

mock.module("next/navigation", () => ({
  useRouter: () => ({ push: () => undefined }),
  usePathname: () => "/",
}));

const { ConfirmProvider } = await import("../shell/confirm");
const { ToastProvider } = await import("../ds");
const { SHELL_EVENTS } = await import("../shell/shell-events");
const { default: AgentWorkbench } = await import("../agent-workbench");

setupDom();

class FakeEventSource {
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  close() {}
}
(globalThis as unknown as { EventSource: unknown }).EventSource =
  FakeEventSource;

const original = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = original;
});

const run = (id: string) => ({
  id,
  status: "waiting_for_review",
  context: {
    repo_id: `local/${id}`,
    episodes: [0],
    cameras: [],
    instruction: "x",
    provider: "external",
    workflow: { kind: "review" },
  },
  completed: [0],
  snapshot_bytes: 0,
  requests: 0,
  tokens: 0,
  reserved_tokens: 0,
  changes: `c-${id}`,
  plan: {
    revision: 1,
    digest: "d",
    approval: { actor: "me" },
    pilot_episode: 0,
    pilot_review: null,
    questions: [],
    estimate: { minimum_requests: 1, tokens: "?" },
    excluded: [],
  },
});

const change = (id: string) => ({
  id,
  revision: 4,
  base_revision: 1,
  status: "draft",
  proposals: [],
  decisions: {},
  object_jobs: [],
});

function stub() {
  globalThis.fetch = mock((url: string, init?: RequestInit) => {
    const href = String(url);
    const json = (value: unknown) =>
      Promise.resolve(new Response(JSON.stringify(value)));
    if (href.endsWith("/runs")) return json([run("a"), run("b")]);
    if (href.endsWith("/manifest"))
      return json({ status: "empty", artifacts: [] });
    if (href.endsWith("/tools")) {
      const body = JSON.parse(String(init?.body));
      if (body.name === "changes.diff")
        return json(change(body.arguments.changeset_id));
      if (body.name === "media.sample") return json({ items: [] });
      if (body.name === "runs.events") return json({ events: [] });
      return json({});
    }
    return json([]);
  }) as unknown as typeof fetch;
}

async function openDrawer() {
  const view = await render(
    <ToastProvider>
      <ConfirmProvider>
        <AgentWorkbench />
      </ConfirmProvider>
    </ToastProvider>,
  );
  await act(async () => {
    window.dispatchEvent(new Event(SHELL_EVENTS.agentToggle));
  });
  await flush(10);
  return view;
}

async function choose(select: HTMLSelectElement, value: string) {
  await act(async () => {
    select.value = value;
    select.dispatchEvent(new Event("change", { bubbles: true }));
  });
}

async function type(field: HTMLTextAreaElement, text: string) {
  await act(async () => {
    const set = Object.getOwnPropertyDescriptor(
      HTMLTextAreaElement.prototype,
      "value",
    )!.set!;
    field.focus();
    set.call(field, text);
    field.dispatchEvent(new Event("input", { bubbles: true }));
    // This DOM hands React a controlled field's value on keyup.
    field.dispatchEvent(
      new KeyboardEvent("keyup", { bubbles: true, key: "e" }),
    );
  });
}

const taskSelect = () =>
  Array.from(document.querySelectorAll("select")).find((s) =>
    Array.from(s.options).some((o) => o.value === "a"),
  ) as HTMLSelectElement | undefined;
const noteField = () =>
  document.querySelector(
    'textarea[placeholder^="Check labels"]',
  ) as HTMLTextAreaElement | null;

const acceptButton = () =>
  Array.from(document.querySelectorAll("button")).find(
    (b) => b.textContent?.trim() === "Accept pilot quality",
  ) as HTMLButtonElement;

describe("switching tasks", () => {
  test("a pilot review note typed for one run does not carry over to the next", async () => {
    stub();
    await openDrawer();
    const select = await waitFor(taskSelect, { label: "the task list" });
    await choose(select, "a");
    const first = await waitFor(noteField, { label: "the pilot note" });
    await type(first, "looks fine for run a");
    expect(noteField()!.value).toBe("looks fine for run a");
    // The typed text reached React state: it enables the accept button.
    expect(acceptButton().disabled).toBe(false);
    await choose(taskSelect()!, "b");
    const second = await waitFor(
      () => {
        const field = noteField();
        return field && document.body.textContent?.includes("local/b")
          ? field
          : null;
      },
      { label: "run b's note" },
    );
    expect(second.value).toBe("");
    expect(acceptButton().disabled).toBe(true);
  });
});
