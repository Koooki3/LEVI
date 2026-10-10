import { click, render, setupDom } from "@/components/ds/__tests__/dom";
import { describe, expect, mock, test } from "bun:test";
import { act } from "react";
import { ToastProvider } from "@/components/ds";
import type { DeletionPlan, ManagementRequest } from "../management-actions";

mock.module("next/link", () => ({
  default: ({ children, ...props }: Record<string, unknown>) => (
    <a {...(props as object)}>{children as never}</a>
  ),
}));
const { LiveDeleteButton, ViewerAction } =
  await import("../management-actions");
setupDom();

const plan: DeletionPlan = {
  confirmation: "snapshot-token",
  files: 2,
  bytes: 1048576,
  paths: ["captures/demo_0001", "views/demo_0001.mp4"],
};
const session = {
  kind: "session" as const,
  root: "/captures",
  group: "pi05",
  task_folder: "watermelon",
  session_id: "session-1",
};
const button = (host: HTMLElement, text: string) =>
  Array.from(host.querySelectorAll("button")).find(
    (node) => node.textContent?.trim() === text,
  ) ?? null;

describe("live session and pipeline deletion", () => {
  test("file cleanup failure remains visible after the pipeline row disappears", async () => {
    const request: ManagementRequest = async <T,>(method: "POST" | "DELETE") =>
      (method === "POST"
        ? plan
        : { deleted: true, cleanup_pending: true }) as T;
    const { host, rerender } = await render(
      <ToastProvider>
        <LiveDeleteButton
          target={{ kind: "dataset", name: "pi05__watermelon" }}
          title="pi05 / watermelon"
          request={request}
        />
      </ToastProvider>,
    );
    await click(button(host, "Delete pipeline"));
    await click(host.querySelector("input[type=checkbox]"));
    await click(button(host, "Preview deletion"));
    await click(button(host, "Delete"));
    await rerender(
      <ToastProvider>
        <p>Removed row</p>
      </ToastProvider>,
    );
    expect(host.textContent).toContain(
      "Pipeline removed, but some files still need cleanup.",
    );
    expect(host.textContent).toContain("pi05 / watermelon");
    expect(button(host, "Delete pipeline")).toBeNull();
  });
  test("a session deletion previews exact identity, then needs a second confirmation", async () => {
    const calls: { method: string; path: string; body: unknown }[] = [];
    let changed = 0;
    const request: ManagementRequest = async <T,>(
      method: "POST" | "DELETE",
      path: string,
      body: unknown,
    ) => {
      calls.push({ method, path, body });
      return (method === "POST" ? plan : {}) as T;
    };
    const { host } = await render(
      <LiveDeleteButton
        target={session}
        title="pi05 / watermelon"
        request={request}
        onChanged={() => changed++}
      />,
    );
    await click(button(host, "Delete session"));
    expect(calls).toEqual([
      {
        method: "POST",
        path: "live/sessions/deletion-plan",
        body: {
          root: "/captures",
          group: "pi05",
          task_folder: "watermelon",
          session_id: "session-1",
        },
      },
    ]);
    expect(host.querySelector("[role=alertdialog]")).toBeTruthy();
    expect(host.textContent).toContain("2 file(s), 1.00 MiB");
    expect(host.textContent?.toLowerCase()).toContain("original rollout files");
    expect(changed).toBe(0);
    await click(button(host, "Delete"));
    expect(calls[1]).toEqual({
      method: "DELETE",
      path: "live/sessions",
      body: {
        root: "/captures",
        group: "pi05",
        task_folder: "watermelon",
        session_id: "session-1",
        confirmation: "snapshot-token",
      },
    });
    expect(changed).toBe(1);
    expect(host.querySelector("[role=alertdialog]")).toBeNull();
  });
  test("pipeline record deletion defaults to keeping files, cancelling sends no DELETE", async () => {
    const calls: unknown[] = [];
    const request: ManagementRequest = async <T,>(
      method: "POST" | "DELETE",
      path: string,
      body: unknown,
    ) => {
      calls.push({ method, path, body });
      return plan as T;
    };
    const { host } = await render(
      <LiveDeleteButton
        target={{ kind: "dataset", name: "pi05__watermelon" }}
        title="pi05 / watermelon"
        request={request}
      />,
    );
    await click(button(host, "Delete pipeline"));
    expect(
      (host.querySelector("input[type=checkbox]") as HTMLInputElement).checked,
    ).toBe(false);
    expect(calls).toEqual([]);
    await click(button(host, "Preview deletion"));
    expect(calls).toEqual([
      {
        method: "POST",
        path: "live/datasets/pi05__watermelon/deletion-plan",
        body: { delete_files: false },
      },
    ]);
    expect(host.textContent).toContain("Only the pipeline record");
    await click(button(host, "Cancel"));
    expect(calls).toHaveLength(1);
  });
  test("explicitly selected mirror deletion is preserved from preview to execution", async () => {
    const calls: { method: string; body: unknown }[] = [];
    const request: ManagementRequest = async <T,>(
      method: "POST" | "DELETE",
      _path: string,
      body: unknown,
    ) => {
      calls.push({ method, body });
      return plan as T;
    };
    const { host } = await render(
      <LiveDeleteButton
        target={{ kind: "dataset", name: "pi05__watermelon" }}
        title="pipeline"
        request={request}
      />,
    );
    await click(button(host, "Delete pipeline"));
    await click(host.querySelector("input[type=checkbox]"));
    await click(button(host, "Preview deletion"));
    expect(calls[0].body).toEqual({ delete_files: true });
    expect(host.textContent).toContain(
      "mirrored data and viewer files will be deleted",
    );
    await click(button(host, "Delete"));
    expect(calls[1].body).toEqual({
      delete_files: true,
      confirmation: "snapshot-token",
    });
  });
  test("busy refusals remain visible and never open a destructive confirmation", async () => {
    const request: ManagementRequest = async () => {
      throw new Error("HTTP 409: episode export is running");
    };
    const { host } = await render(
      <LiveDeleteButton target={session} title="session" request={request} />,
    );
    await click(button(host, "Delete session"));
    expect(host.querySelector("[role=alert]")?.textContent).toContain(
      "export is running",
    );
    expect(host.querySelector("[role=alertdialog]")).toBeNull();
  });
  test("double clicks cannot issue duplicate previews or deletions", async () => {
    let calls = 0;
    let settle: (value: unknown) => void = () => {};
    const request: ManagementRequest = <T,>() => {
      calls++;
      return new Promise<T>((resolve) => {
        settle = resolve as (value: unknown) => void;
      });
    };
    const { host } = await render(
      <LiveDeleteButton target={session} title="session" request={request} />,
    );
    await act(async () => {
      const trigger = button(host, "Delete session")!;
      trigger.click();
      trigger.click();
    });
    expect(calls).toBe(1);
    await act(async () => settle(plan));
    await act(async () => {
      const trigger = button(host, "Delete")!;
      trigger.click();
      trigger.click();
    });
    expect(calls).toBe(2);
    await act(async () => settle({}));
  });
  test("blocked actions are disabled with a visible reason", async () => {
    const { host } = await render(
      <LiveDeleteButton
        target={session}
        title="session"
        blocked="A running evaluation session cannot be deleted."
      />,
    );
    expect(button(host, "Delete session")?.disabled).toBe(true);
    expect(host.textContent).toContain(
      "A running evaluation session cannot be deleted.",
    );
  });
});

describe("viewer routing", () => {
  test("a prepared viewer URL is used verbatim, including its session scope", async () => {
    const { host } = await render(
      <ViewerAction
        url="/local/pi05/5?live_session=session-1"
        status="ready"
      />,
    );
    expect(host.querySelector("a")?.getAttribute("href")).toBe(
      "/local/pi05/5?live_session=session-1",
    );
  });
  test("an in-progress view is unavailable with a visible explanation", async () => {
    const { host } = await render(
      <ViewerAction url="/local/pi05/5" status="preparing" />,
    );
    expect(host.querySelector("a")).toBeNull();
    expect(host.querySelector("button")?.disabled).toBe(true);
    expect(host.textContent).toContain("The dataset viewer is being prepared.");
  });
  test("pending views can be prepared, then wait without issuing duplicate requests", async () => {
    const calls: unknown[] = [];
    let changed = 0;
    const request: ManagementRequest = async <T,>(
      method: "POST" | "DELETE",
      path: string,
      body: unknown,
    ) => {
      calls.push({ method, path, body });
      return { running: true, started: true } as T;
    };
    const { host } = await render(
      <ViewerAction
        status="pending"
        dataset="pi05__watermelon"
        request={request}
        onChanged={() => changed++}
      />,
    );
    expect(host.querySelector("a")).toBeNull();
    await click(button(host, "Prepare dataset viewer"));
    expect(calls).toEqual([
      {
        method: "POST",
        path: "live/datasets/pi05__watermelon/prepare",
        body: {},
      },
    ]);
    expect(changed).toBe(1);
    expect(host.textContent).toContain("The dataset viewer is being prepared.");
    expect(button(host, "Prepare dataset viewer")).toBeNull();
  });
  test("a failed retry becomes available again when the catalogue timestamp advances", async () => {
    const request: ManagementRequest = async <T,>() =>
      ({ running: true, started: true }) as T;
    const { host, rerender } = await render(
      <ViewerAction
        status="error"
        dataset="pi05__watermelon"
        updatedAt={10}
        request={request}
      />,
    );
    await click(button(host, "Prepare dataset viewer"));
    expect(host.textContent).toContain("The dataset viewer is being prepared.");
    expect(button(host, "Prepare dataset viewer")).toBeNull();
    await rerender(
      <ViewerAction
        status="error"
        dataset="pi05__watermelon"
        updatedAt={11}
        request={request}
      />,
    );
    expect(host.textContent).toContain(
      "The dataset viewer could not be prepared.",
    );
    expect(button(host, "Prepare dataset viewer")).toBeTruthy();
    expect(host.textContent).not.toContain(
      "The dataset viewer is being prepared.",
    );
  });
  test("a prepare that starts no running worker stays available for another attempt", async () => {
    let calls = 0;
    const request: ManagementRequest = async <T,>() => {
      calls++;
      return { running: false, started: false } as T;
    };
    const { host } = await render(
      <ViewerAction
        status="pending"
        dataset="pi05__watermelon"
        request={request}
      />,
    );
    await click(button(host, "Prepare dataset viewer"));
    expect(
      button(host, "Prepare dataset viewer")?.getAttribute("aria-busy"),
    ).toBeNull();
    expect(host.textContent).not.toContain(
      "The dataset viewer is being prepared.",
    );
    await click(button(host, "Prepare dataset viewer"));
    expect(calls).toBe(2);
  });
  test("a refused prepare reports the 409 reason and clears its local busy state", async () => {
    let calls = 0;
    const request: ManagementRequest = async () => {
      calls++;
      throw new Error("HTTP 409: no finished episodes can be safely prepared");
    };
    const { host } = await render(
      <ViewerAction
        status="error"
        dataset="pi05__watermelon"
        request={request}
      />,
    );
    await click(button(host, "Prepare dataset viewer"));
    expect(host.querySelector("[role=alert]")?.textContent).toContain(
      "no finished episodes",
    );
    expect(
      button(host, "Prepare dataset viewer")?.getAttribute("aria-busy"),
    ).toBeNull();
    expect(host.textContent).not.toContain(
      "The dataset viewer is being prepared.",
    );
    await click(button(host, "Prepare dataset viewer"));
    expect(calls).toBe(2);
  });
});
