import { describe, expect, mock, test, afterEach } from "bun:test";
import { render, setupDom, waitFor } from "@/components/ds/__tests__/dom";
import type { LiveSession } from "../types";
import { identityOf } from "../session-route";
const session: LiveSession = {
  root: "/fixture",
  group: "model",
  task_folder: "task",
  run_id: "run-1",
  session_id: "session-1",
  dataset: "model__task",
  state: "finished",
  policy: { checkpoint: "wanted-checkpoint" },
};
let current: LiveSession[] = [session];
let failures = 0;
mock.module("next/link", () => ({
  default: ({ children, ...props }: Record<string, unknown>) => (
    <a {...(props as object)}>{children as never}</a>
  ),
}));
mock.module("../use-live", () => ({
  useLivePoll: () => ({
    sessions: { sessions: current },
    status: { enabled: true },
    lastOk: 100,
    failures,
    refresh: () => {},
  }),
}));
const { SessionDetail } = await import("../session-detail");
setupDom();
const originalFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = originalFetch;
  current = [session];
  failures = 0;
});
describe("individual live session", () => {
  test("reads statistics for the run id and exact dataset and keeps navigation separate", async () => {
    const calls: string[] = [];
    globalThis.fetch = (async (path: unknown) => {
      calls.push(String(path));
      return new Response(
        JSON.stringify({
          enabled: true,
          summary: { episodes: { count: 1 } },
          episodes: { rows: [], total: 0 },
        }),
      );
    }) as typeof fetch;
    const { host } = await render(
      <SessionDetail identity={identityOf(session)} />,
    );
    await waitFor(() => calls.length > 0);
    expect(calls[0]).toContain("dataset=model__task");
    expect(calls[0]).toContain("session=run-1");
    expect(calls[0]).not.toContain("session=session-1");
    expect(host.textContent).toContain("wanted-checkpoint");
    expect(host.querySelector('a[href="/live"]')).not.toBeNull();
    expect(host.querySelector("button[aria-expanded]")).toBeNull();
  });
  test("a replaced current snapshot never becomes the old session but saved records remain readable", async () => {
    current = [
      {
        ...session,
        session_id: "session-2",
        run_id: "run-2",
        policy: { checkpoint: "wrong-checkpoint" },
      },
    ];
    globalThis.fetch = (async () =>
      new Response(
        JSON.stringify({
          enabled: true,
          summary: { episodes: { count: 1 } },
          episodes: {
            rows: [{ demo: "saved-episode", episode_index: 7, state: "done" }],
            total: 1,
          },
        }),
      )) as typeof fetch;
    const { host } = await render(
      <SessionDetail identity={identityOf(session)} />,
    );
    await waitFor(() => host.textContent?.includes("saved-episode"));
    expect(host.textContent).not.toContain("wrong-checkpoint");
    expect(host.textContent).toContain("no longer reporting");
  });
  test("offline status is stated while history can load", async () => {
    current = [];
    failures = 1;
    globalThis.fetch = (async () =>
      new Response(
        JSON.stringify({ enabled: true, summary: { episodes: { count: 0 } } }),
      )) as typeof fetch;
    const { host } = await render(
      <SessionDetail identity={identityOf(session)} />,
    );
    expect(host.textContent).toContain("Live status is unavailable");
    await waitFor(() =>
      host.textContent?.includes("No labelled episode matches"),
    );
  });
  test("invalid bookmarks are explained and have a return link", async () => {
    const { host } = await render(<SessionDetail identity={null} />);
    expect(host.textContent).toContain("Invalid session link");
    expect(host.querySelector('a[href="/live"]')).not.toBeNull();
  });
});
