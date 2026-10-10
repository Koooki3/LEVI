import { describe, expect, test, mock } from "bun:test";
import { act } from "react";
import { render, setupDom } from "@/components/ds/__tests__/dom";
import { episodeHref, useLocalEpisodes } from "../use-local-episodes";

setupDom();

function Probe({
  dataset = "demo",
  session = null,
  revision = 0,
  inspect,
}: {
  dataset?: string;
  session?: string | null;
  revision?: number;
  inspect?: (dataset: string, ids: number[] | null) => void;
}) {
  const state = useLocalEpisodes("local", dataset, session, revision);
  inspect?.(dataset, state.indices);
  return (
    <div>
      {state.loading ? "loading" : state.error || JSON.stringify(state.indices)}
    </div>
  );
}

async function flush() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

describe("authoritative local episode navigation", () => {
  test("retains real sparse IDs and encoded session scope", async () => {
    const original = globalThis.fetch;
    const fetcher = mock(async (url: RequestInfo | URL) => {
      expect(url).toBe(
        "/api/levi/datasets/demo/episodes?live_session=run%3Aa%2Fb",
      );
      return Response.json({ indices: [2, 7] });
    });
    globalThis.fetch = fetcher as typeof fetch;
    try {
      const { host } = await render(<Probe session="run:a/b" />);
      await flush();
      expect(host.textContent).toBe("[2,7]");
      expect(fetcher.mock.calls[0][0]).toBe(
        "/api/levi/datasets/demo/episodes?live_session=run%3Aa%2Fb",
      );
      expect(episodeHref(7, "run:a/b")).toBe(
        "./episode_7?live_session=run%3Aa%2Fb",
      );
    } finally {
      globalThis.fetch = original;
    }
  });

  test("scope change never exposes previous dataset IDs and ignores its late response", async () => {
    const original = globalThis.fetch;
    const pending: ((response: Response) => void)[] = [];
    globalThis.fetch = (() =>
      new Promise((resolve) => pending.push(resolve))) as typeof fetch;
    const seen: [string, number[] | null][] = [];
    try {
      const view = await render(
        <Probe
          dataset="first"
          inspect={(dataset, ids) => seen.push([dataset, ids])}
        />,
      );
      await view.rerender(
        <Probe
          dataset="second"
          inspect={(dataset, ids) => seen.push([dataset, ids])}
        />,
      );
      await act(async () => {
        pending[0](Response.json({ indices: [1] }));
        pending[1](Response.json({ indices: [9] }));
      });
      await flush();
      expect(view.host.textContent).toBe("[9]");
      expect(
        seen.some(([dataset, ids]) => dataset === "second" && ids?.includes(1)),
      ).toBe(false);
    } finally {
      globalThis.fetch = original;
    }
  });

  test("Retry revision refetches an index request that failed", async () => {
    const original = globalThis.fetch;
    let calls = 0;
    globalThis.fetch = (async () =>
      ++calls === 1
        ? Response.json({ detail: "temporary failure" }, { status: 503 })
        : Response.json({ indices: [8] })) as typeof fetch;
    try {
      const view = await render(<Probe />);
      await flush();
      expect(view.host.textContent).toBe("temporary failure");
      await view.rerender(<Probe revision={1} />);
      await flush();
      expect(view.host.textContent).toBe("[8]");
      expect(calls).toBe(2);
    } finally {
      globalThis.fetch = original;
    }
  });

  test("an empty selection stays empty and never invents episode zero", async () => {
    const original = globalThis.fetch;
    globalThis.fetch = (async () =>
      Response.json({ indices: [] })) as typeof fetch;
    try {
      const { host } = await render(<Probe session="empty-run" />);
      await flush();
      expect(host.textContent).toBe("[]");
    } finally {
      globalThis.fetch = original;
    }
  });
});
