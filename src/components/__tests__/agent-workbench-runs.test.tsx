import { act } from "react";
import { click, flush, render, setupDom, waitFor } from "../ds/__tests__/dom";
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

const toolCalls: string[] = [];
function stub() {
  toolCalls.length = 0;
  globalThis.fetch = mock((url: string, init?: RequestInit) => {
    const href = String(url);
    const json = (value: unknown) =>
      Promise.resolve(new Response(JSON.stringify(value)));
    if (href.endsWith("/runs")) return json([run("a"), run("b")]);
    if (href.endsWith("/manifest"))
      return json({ status: "empty", artifacts: [] });
    if (href.endsWith("/tools")) {
      const body = JSON.parse(String(init?.body));
      toolCalls.push(body.name);
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

describe("a failed background read", () => {
  test("says the list could not be refreshed, without 'Error:', and clears when a read works", async () => {
    let healthy = false;
    stub();
    const working = globalThis.fetch;
    globalThis.fetch = mock((url: string, init?: RequestInit) =>
      !healthy && String(url).endsWith("/runs")
        ? Promise.resolve(
            new Response(JSON.stringify({ detail: "service is restarting" }), {
              status: 503,
            }),
          )
        : (working as unknown as typeof fetch)(url, init),
    ) as unknown as typeof fetch;
    await openDrawer();
    await waitFor(
      () =>
        document.body.textContent?.includes(
          "Task list could not be refreshed",
        ) || false,
      { label: "the refresh banner" },
    );
    const text = document.body.textContent ?? "";
    expect(text).toContain("service is restarting");
    expect(text).not.toContain("Error: ");
    expect(text).not.toContain("That action did not complete");
    healthy = true;
    // The next poll (every 2.5 s) clears it; a tab change re-runs the read.
    await waitFor(
      () =>
        !document.body.textContent?.includes(
          "Task list could not be refreshed",
        ),
      { label: "the banner to clear", timeoutMs: 6000 },
    );
  });
});

describe("Cancel task", () => {
  const press = (root: ParentNode, label: string) =>
    Array.from(root.querySelectorAll("button")).find(
      (b) => b.textContent?.trim() === label,
    ) as HTMLButtonElement;

  test("asks first: keeping the task sends nothing, cancelling sends runs.cancel", async () => {
    stub();
    await openDrawer();
    await choose(await waitFor(taskSelect, { label: "the task list" }), "a");
    await waitFor(() => press(document, "Cancel task"), {
      label: "the Cancel task button",
    });
    await click(press(document, "Cancel task"));
    const dialog = document.querySelector('[role="alertdialog"]')!;
    expect(dialog.textContent).toContain("Cancel this task?");
    await click(press(dialog, "Keep the task"));
    await flush(10);
    expect(toolCalls).not.toContain("runs.cancel");
    await click(press(document, "Cancel task"));
    await click(
      press(document.querySelector('[role="alertdialog"]')!, "Cancel task"),
    );
    await flush(10);
    expect(toolCalls.filter((n) => n === "runs.cancel").length).toBe(1);
  });
});

describe("the open-task shell event", () => {
  const send = (runId?: string) =>
    act(async () => {
      window.dispatchEvent(
        new CustomEvent(SHELL_EVENTS.agentOpenTask, { detail: { runId } }),
      );
    });
  const sheetOpen = () => taskSelect() !== undefined;

  test("opens the drawer on the task tab with that run selected, and never closes it", async () => {
    stub();
    await render(
      <ToastProvider>
        <ConfirmProvider>
          <AgentWorkbench />
        </ConfirmProvider>
      </ToastProvider>,
    );
    expect(sheetOpen()).toBe(false);
    await send("b");
    await waitFor(
      () =>
        document.body.textContent?.includes("local/b") &&
        taskSelect()?.value === "b",
      { label: "run b selected" },
    );
    // A second press (the toggle would close it) leaves it open.
    await send("b");
    await flush(10);
    expect(sheetOpen()).toBe(true);
    expect(taskSelect()!.value).toBe("b");
    // Without a run it still opens, on the same tab, keeping the selection.
    await send();
    expect(taskSelect()!.value).toBe("b");
  });
});
