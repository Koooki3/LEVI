import { click, render, setupDom } from "@/components/ds/__tests__/dom";
import { describe, expect, mock, test } from "bun:test";
import { useState } from "react";
import type { DatasetRow, LiveSession } from "../types";

mock.module("next/link", () => ({
  default: ({ children, ...props }: Record<string, unknown>) => (
    <a {...(props as object)}>{children as never}</a>
  ),
}));
const { DatasetCard } = await import("../dataset-card");
const { SessionsPanel } = await import("../session-panels");

setupDom();

const session: LiveSession = {
  group: "pi05",
  task_folder: "watermelon",
  state: "finished",
  session_id: "session-1",
  run_id: "run-1",
  root: "/test/rollouts",
  prompt: "pick watermelon on the bread",
  viewer_url: "/local/watermelon/5?live_session=session-1",
  view_status: "ready",
  policy: { checkpoint: "checkpoint-detail" },
};
const row: DatasetRow = {
  episodes: 2,
  done: 1,
  pending: 1,
  annotating: 0,
  failed: 0,
  state: "pending",
  viewer_url: "/local/watermelon/5",
  view_status: "ready",
};

function Pipeline() {
  const [open, setOpen] = useState(false);
  return (
    <DatasetCard
      name="pi05__watermelon"
      row={row}
      entry={{
        data: {
          enabled: true,
          name: "pi05__watermelon",
          demos: [{ demo: "demo_0005", state: "done" }],
        },
        at: 0,
        signature: "",
        error: "",
      }}
      fault={null}
      open={open}
      onToggle={() => setOpen((value) => !value)}
      workerPhase={null}
      filter="latest"
      onFilter={() => {}}
      nowSeconds={0}
      onChanged={() => {}}
    />
  );
}

describe("compact live rows", () => {
  test("sessions start collapsed and expose a real disclosure button", async () => {
    const { host } = await render(<SessionsPanel sessions={[session]} />);
    const toggle = host.querySelector("button[aria-expanded]")!;
    expect(toggle.tagName).toBe("BUTTON");
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(host.textContent).toContain("run-1");
    expect(host.textContent).not.toContain("checkpoint-detail");
    expect(host.querySelector("[role=region]")).toBeNull();
    await click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(host.textContent).toContain("checkpoint-detail");
    expect(host.querySelector("[role=region]")?.id).toBe(
      toggle.getAttribute("aria-controls") ?? undefined,
    );
    await click(toggle);
    expect(host.querySelector("[role=region]")).toBeNull();
  });
  test("viewer actions preserve the session scope and do not expand the summary", async () => {
    const { host } = await render(<SessionsPanel sessions={[session]} />);
    const link = host.querySelector("a")!;
    expect(link.getAttribute("href")).toBe(session.viewer_url ?? null);
    expect(link.closest("button[aria-expanded]")).toBeNull();
    await click(link);
    expect(
      host
        .querySelector("button[aria-expanded]")
        ?.getAttribute("aria-expanded"),
    ).toBe("false");
    const deletion = Array.from(host.querySelectorAll("button")).find(
      (button) => button.textContent?.includes("Delete session"),
    )!;
    expect(deletion.closest("button[aria-expanded]")).toBeNull();
  });
  test("pipelines start collapsed, expanding reveals their statistics and episodes", async () => {
    const { host } = await render(<Pipeline />);
    expect(host.textContent).not.toContain("Time segments");
    expect(host.textContent).not.toContain("demo_0005");
    await click(host.querySelector("button[aria-expanded]"));
    expect(host.textContent).toContain("Time segments");
    expect(host.textContent).toContain("demo_0005");
  });
  test("no-match feedback distinguishes filters from an empty workspace", async () => {
    const { host } = await render(
      <SessionsPanel sessions={[]} total={40} filtered />,
    );
    expect(host.textContent).toContain(
      "No evaluation sessions match these filters.",
    );
    expect(host.textContent).toContain("0 / 40");
  });
});
