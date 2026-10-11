import { describe, expect, mock, test, afterEach } from "bun:test";
import {
  click,
  render,
  setupDom,
  waitFor,
} from "@/components/ds/__tests__/dom";
import type { LiveSession } from "../types";
import { resultFixture } from "./result-fixture";
import { identityOf } from "../session-route";
import {
  statisticsFixture,
  statisticsSourceFixture,
} from "./statistics-fixture";
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
let lastOk: number | null = 100;
mock.module("next/link", () => ({
  default: ({ children, ...props }: Record<string, unknown>) => (
    <a {...(props as object)}>{children as never}</a>
  ),
}));
mock.module("../use-live", () => ({
  useLivePoll: () => ({
    sessions: { sessions: current },
    status: { enabled: true },
    lastOk,
    failures,
    refresh: () => {},
  }),
}));
const { SessionDetail } = await import("../session-detail");
setupDom();
const originalFetch = globalThis.fetch;
const originalNow = Date.now;
afterEach(() => {
  globalThis.fetch = originalFetch;
  Date.now = originalNow;
  current = [session];
  failures = 0;
  lastOk = 100;
});
describe("individual live session", () => {
  for (const [name, bad] of [
    [
      "foreign aggregate",
      {
        scope: { dataset: "foreign-blind-source", session: "run-1" },
        datasets: ["foreign-blind-source"],
      },
    ],
    [
      "foreign session",
      { scope: { dataset: "model__task", session: "other-run" } },
    ],
    ["missing actual source identities", { source_identities: undefined }],
    ["empty actual source identities", { source_identities: [] }],
    [
      "foreign root with identical dataset and run",
      {
        source_identities: [statisticsSourceFixture({ root: "/foreign-root" })],
      },
    ],
    [
      "foreign group with identical dataset and run",
      {
        source_identities: [statisticsSourceFixture({ group: "other-group" })],
      },
    ],
    [
      "foreign task with identical dataset and run",
      { source_identities: [statisticsSourceFixture({ task: "other-task" })] },
    ],
    [
      "foreign canonical run",
      { source_identities: [statisticsSourceFixture({ run_id: "other-run" })] },
    ],
    [
      "foreign canonical session id",
      {
        source_identities: [
          statisticsSourceFixture({ session_id: "session-2" }),
        ],
      },
    ],
    [
      "null session when canonical is known",
      { source_identities: [statisticsSourceFixture({ session_id: null })] },
    ],
    [
      "source misses selected dataset",
      { source_identities: [statisticsSourceFixture({ datasets: [] })] },
    ],
    [
      "source contains unknown dataset",
      {
        source_identities: [
          statisticsSourceFixture({
            datasets: ["model__task", "foreign-blind-source"],
          }),
        ],
      },
    ],
    [
      "multiple sources include foreign root",
      {
        source_identities: [
          statisticsSourceFixture(),
          statisticsSourceFixture({ root: "/foreign-root" }),
        ],
      },
    ],
    [
      "duplicate canonical identities are ambiguous",
      {
        source_identities: [
          statisticsSourceFixture(),
          statisticsSourceFixture(),
        ],
      },
    ],
    ["missing scope", { scope: undefined }],
    ["missing datasets", { datasets: undefined }],
    ["missing schema", { schema: undefined }],
    ["missing sessions", { sessions: undefined }],
    [
      "missing row dataset",
      {
        episodes: {
          rows: [
            {
              dataset: undefined,
              session: "run-1",
              demo: "foreign-private-episode",
            },
          ],
        },
      },
    ],
    [
      "missing row session field",
      {
        episodes: {
          rows: [
            {
              dataset: "model__task",
              session: undefined,
              demo: "foreign-private-episode",
            },
          ],
        },
      },
    ],
    ["missing rows", { episodes: { total: 1 } }],
    [
      "foreign dataset metadata",
      { datasets: ["model__task", "foreign-blind-source"] },
    ],
    [
      "foreign episode",
      {
        episodes: {
          total: 2,
          rows: [
            { dataset: "model__task", session: "run-1", demo: "allowed-row" },
            {
              dataset: "foreign-blind-source",
              session: "run-1",
              demo: "foreign-private-episode",
            },
          ],
        },
      },
    ],
    [
      "foreign session row",
      { sessions: [{ dataset: "model__task", session: "other-run" }] },
    ],
    [
      "missing row session",
      {
        episodes: {
          rows: [
            {
              dataset: "model__task",
              session: null,
              demo: "foreign-private-episode",
            },
          ],
        },
      },
    ],
    ["wrong schema", { schema: "other.v1" }],
  ] as const) {
    test(`rejects the whole statistics aggregate and export for ${name}`, async () => {
      globalThis.fetch = (async (path: unknown) =>
        String(path).includes("/results/session?")
          ? Response.json(
              resultFixture({ analysis: null, records: [], runs: [] }),
            )
          : Response.json(
              statisticsFixture({
                summary: { episodes: { count: 73 } },
                episodes: {
                  rows: [
                    {
                      demo: "foreign-private-episode",
                      state: "done",
                      operator: "success",
                    },
                  ],
                  total: 1,
                },
                ...bad,
              } as Parameters<typeof statisticsFixture>[0]),
            )) as typeof fetch;
      const { host } = await render(
        <SessionDetail identity={identityOf(session)} />,
      );
      await waitFor(() =>
        host.textContent?.includes("Statistics source could not be verified"),
      );
      expect(host.textContent).not.toContain("foreign-private-episode");
      expect(host.textContent).not.toContain("allowed-row");
      expect(
        host.querySelector("#session-statistics")?.textContent,
      ).not.toContain("73");
      expect(host.querySelector(".pg-live-kpis")).toBeNull();
      expect(host.querySelector('a[href*="/stats/export"]')).toBeNull();
    });
  }

  test("bound export HTTP 409 retains clear feedback and revokes statistics download entries", async () => {
    let changed = false;
    let href = "";
    globalThis.fetch = (async (path: unknown) => {
      const value = String(path);
      if (value.includes("/results/session?"))
        return Response.json(
          resultFixture({ analysis: null, records: [], runs: [] }),
        );
      if (value.includes("/stats/export?")) {
        href = value;
        changed = true;
        return new Response("source changed", { status: 409 });
      }
      return Response.json(
        statisticsFixture({
          source_identities: [
            statisticsSourceFixture({
              root: changed ? "/foreign-root" : "/fixture",
            }),
          ],
          episodes: {
            rows: [{ demo: "verified-row", state: "done" }],
            total: 1,
          },
        }),
      );
    }) as typeof fetch;
    const { host } = await render(
      <SessionDetail identity={identityOf(session)} />,
    );
    await waitFor(() => host.textContent?.includes("verified-row"));
    await click(host.querySelector('a[href*="/stats/export"]'));
    await waitFor(() =>
      host.textContent?.includes(
        "Statistics source changed. Refresh this session before downloading.",
      ),
    );
    await waitFor(() =>
      host.textContent?.includes("Statistics source could not be verified"),
    );
    expect(
      JSON.parse(
        new URL(href, "http://localhost").searchParams.get("expected_sources")!,
      ).identities,
    ).toEqual([statisticsSourceFixture()]);
    expect(host.querySelector('a[href*="/stats/export"]')).toBeNull();
    expect(host.textContent).not.toContain("verified-row");
    expect(
      [...host.querySelectorAll("button")].some(
        (button) => button.textContent === "Refresh",
      ),
    ).toBe(true);
  });

  test("same dataset and run rebound from root A to root B clears the entire aggregate and export", async () => {
    current = [{ ...session, state: "running" }];
    let rebound = false;
    globalThis.fetch = (async (path: unknown) =>
      String(path).includes("/results/session?")
        ? Response.json(
            resultFixture({ analysis: null, records: [], runs: [] }),
          )
        : Response.json(
            statisticsFixture({
              source_identities: [
                statisticsSourceFixture({
                  root: rebound ? "/foreign-root" : "/fixture",
                }),
              ],
              summary: { episodes: { count: rebound ? 73 : 1 } },
              episodes: {
                total: 1,
                rows: [
                  {
                    dataset: "model__task",
                    session: "run-1",
                    demo: rebound
                      ? "foreign-root-private-episode"
                      : "permitted-root-a",
                    state: "done",
                    operator: "success",
                  },
                ],
              },
            }),
          )) as typeof fetch;
    const { host, rerender } = await render(
      <SessionDetail identity={identityOf(session)} />,
    );
    await waitFor(() => host.textContent?.includes("permitted-root-a"));
    expect(host.querySelector('a[href*="/stats/export"]')).not.toBeNull();
    rebound = true;
    Date.now = () => originalNow() + 6000;
    lastOk = 200;
    await rerender(<SessionDetail identity={identityOf(session)} />);
    await waitFor(() =>
      host.textContent?.includes("Statistics source could not be verified"),
    );
    expect(host.textContent).not.toContain("permitted-root-a");
    expect(host.textContent).not.toContain("foreign-root-private-episode");
    expect(
      host.querySelector("#session-statistics")?.textContent,
    ).not.toContain("73");
    expect(host.querySelector(".pg-live-kpis")).toBeNull();
    expect(host.querySelector('a[href*="/stats/export"]')).toBeNull();
  });

  test("same-scope statistics polling revokes rows, aggregate and exports while pending and after HTTP 409", async () => {
    current = [{ ...session, state: "running" }];
    let waiting = false;
    let resolve: (response: Response) => void = () => {};
    globalThis.fetch = ((path: unknown) => {
      if (String(path).includes("/results/session?"))
        return Promise.resolve(
          Response.json(
            resultFixture({ analysis: null, records: [], runs: [] }),
          ),
        );
      return waiting
        ? new Promise<Response>((r) => {
            resolve = r;
          })
        : Promise.resolve(
            Response.json(
              statisticsFixture({
                summary: { episodes: { count: 73 } },
                episodes: {
                  rows: [{ demo: "old-permitted-row", state: "done" }],
                  total: 1,
                },
              }),
            ),
          );
    }) as typeof fetch;
    const { host, rerender } = await render(
      <SessionDetail identity={identityOf(session)} />,
    );
    await waitFor(() => host.textContent?.includes("old-permitted-row"));
    expect(host.querySelector('a[href*="/stats/export"]')).not.toBeNull();
    waiting = true;
    Date.now = () => originalNow() + 6000;
    lastOk = 200;
    await rerender(<SessionDetail identity={identityOf(session)} />);
    expect(host.textContent).not.toContain("old-permitted-row");
    expect(
      host.querySelector("#session-statistics")?.textContent,
    ).not.toContain("73");
    expect(host.querySelector('a[href*="/stats/export"]')).toBeNull();
    resolve(new Response("source changed", { status: 409 }));
    await waitFor(() => host.textContent?.includes("HTTP 409"));
    expect(host.textContent).not.toContain("old-permitted-row");
    expect(host.querySelector('a[href*="/stats/export"]')).toBeNull();
  });
  test("a canonical multi-dataset identity permits metadata choices but every row stays in the selected scope", async () => {
    globalThis.fetch = (async (path: unknown) =>
      String(path).includes("/results/session?")
        ? Response.json(
            resultFixture({
              identity: {
                ...resultFixture().identity,
                datasets: ["model__task", "canonical-secondary"],
              },
            }),
          )
        : Response.json(
            statisticsFixture({
              datasets: ["model__task", "canonical-secondary"],
              episodes: {
                rows: [{ demo: "valid-selected-row", state: "done" }],
                total: 1,
              },
            }),
          )) as typeof fetch;
    const { host } = await render(
      <SessionDetail identity={identityOf(session)} />,
    );
    await waitFor(() => host.textContent?.includes("valid-selected-row"));
    expect(host.querySelector('a[href*="/stats/export"]')).not.toBeNull();
  });

  test("reads statistics for the run id and exact dataset and keeps navigation separate", async () => {
    const calls: string[] = [];
    globalThis.fetch = (async (path: unknown) => {
      calls.push(String(path));
      if (String(path).includes("/results/session?"))
        return Response.json(resultFixture());
      return new Response(
        JSON.stringify({
          enabled: true,
          schema: "levi.live.stats.v1",
          source_identities: [statisticsSourceFixture()],
          scope: { dataset: "model__task", session: "run-1" },
          include_excluded: false,
          datasets: ["model__task"],
          sessions: [],
          summary: { episodes: { count: 1 } },
          episodes: { rows: [], total: 0 },
        }),
      );
    }) as typeof fetch;
    const { host } = await render(
      <SessionDetail identity={identityOf(session)} />,
    );
    await waitFor(() => calls.some((path) => path.includes("/stats?")));
    const statsCall = calls.find((path) => path.includes("/stats?"))!;
    expect(statsCall).toContain("dataset=model__task");
    expect(statsCall).toContain("session=run-1");
    expect(statsCall).not.toContain("session=session-1");
    expect(host.textContent).toContain("wanted-checkpoint");
    expect(host.querySelector('a[href="/live"]')).not.toBeNull();
    expect(host.querySelector("button[aria-expanded]") === null).toBe(true);
  });
  test("a rebound current source never exposes old outcome statistics", async () => {
    current = [
      {
        ...session,
        session_id: "session-2",
        run_id: "run-2",
        policy: { checkpoint: "wrong-checkpoint" },
      },
    ];
    globalThis.fetch = (async (path: unknown) =>
      String(path).includes("/results/session?")
        ? Response.json(
            resultFixture({
              status: "rebound",
              result_visibility: "unknown",
              analysis: null,
              records: [],
              runs: [],
              reports: [],
            }),
          )
        : new Response(
            JSON.stringify({
              enabled: true,
              schema: "levi.live.stats.v1",
              source_identities: [statisticsSourceFixture()],
              scope: { dataset: "model__task", session: "run-1" },
              include_excluded: false,
              datasets: ["model__task"],
              sessions: [],
              summary: { episodes: { count: 1 } },
              episodes: {
                rows: [
                  {
                    dataset: "model__task",
                    session: "run-1",
                    demo: "saved-episode",
                    episode_index: 7,
                    state: "done",
                  },
                ],
                total: 1,
              },
            }),
          )) as typeof fetch;
    const { host } = await render(
      <SessionDetail identity={identityOf(session)} />,
    );
    await waitFor(() => host.textContent?.includes("Session source changed"));
    expect(host.textContent).not.toContain("saved-episode");
    expect(host.querySelector('a[href*="/stats/export"]') === null).toBe(true);
    expect(host.textContent).not.toContain("wrong-checkpoint");
    expect(host.textContent).toContain("no longer reporting");
  });
  test("offline status is stated while history can load", async () => {
    current = [];
    failures = 1;
    globalThis.fetch = (async (path: unknown) =>
      String(path).includes("/results/session?")
        ? Response.json(resultFixture())
        : new Response(
            JSON.stringify({
              enabled: true,
              schema: "levi.live.stats.v1",
              source_identities: [statisticsSourceFixture()],
              scope: { dataset: "model__task", session: "run-1" },
              include_excluded: false,
              datasets: ["model__task"],
              sessions: [],
              summary: { episodes: { count: 0 } },
              episodes: { total: 0, rows: [] },
            }),
          )) as typeof fetch;
    const { host } = await render(
      <SessionDetail identity={identityOf(session)} />,
    );
    expect(host.textContent).toContain("Live status is unavailable");
    await waitFor(() =>
      host.textContent?.includes("No labelled episode matches"),
    );
  });
  test("manual statistics retry recovers while live remains offline with no heartbeat", async () => {
    current = [];
    failures = 3;
    lastOk = null;
    let ready = false;
    let calls = 0;
    globalThis.fetch = (async (path: unknown) => {
      if (String(path).includes("/results/session?"))
        return Response.json(resultFixture());
      calls++;
      return ready
        ? new Response(
            JSON.stringify({
              enabled: true,
              schema: "levi.live.stats.v1",
              source_identities: [statisticsSourceFixture()],
              scope: { dataset: "model__task", session: "run-1" },
              include_excluded: false,
              datasets: ["model__task"],
              sessions: [],
              summary: { episodes: { count: 1 } },
              episodes: {
                rows: [
                  {
                    dataset: "model__task",
                    session: "run-1",
                    demo: "recovered-history",
                    state: "done",
                  },
                ],
                total: 1,
              },
            }),
          )
        : new Response(JSON.stringify({ detail: "stats offline" }), {
            status: 503,
          });
    }) as typeof fetch;
    const { host } = await render(
      <SessionDetail identity={identityOf(session)} />,
    );
    await waitFor(() =>
      host.textContent?.includes("Could not read the statistics"),
    );
    expect(calls).toBe(1);
    ready = true;
    await click(
      [...host.querySelectorAll("button")].find(
        (b) => b.textContent === "Refresh",
      )!,
    );
    await waitFor(() => host.textContent?.includes("recovered-history"));
    expect(calls).toBe(2);
    expect(host.textContent).toContain("Live status is unavailable");
    expect(host.textContent).not.toContain("Could not read the statistics");
  });
  test("explicit withheld visibility prevents even requesting legacy outcome statistics", async () => {
    const calls: string[] = [];
    globalThis.fetch = (async (path: unknown) => {
      calls.push(String(path));
      return Response.json(
        resultFixture({ status: "blocked", result_visibility: "withheld" }),
      );
    }) as typeof fetch;
    const { host } = await render(
      <SessionDetail identity={identityOf(session)} />,
    );
    await waitFor(() => host.textContent?.includes("Result access blocked"));
    expect(calls.some((path) => path.includes("/stats?"))).toBe(false);
    expect(host.querySelector('a[href*="/stats/export"]') === null).toBe(true);
    expect(host.querySelector("meter") === null).toBe(true);
    expect(host.textContent).not.toContain("demo-7");
  });
  test("a failed result lookup never falls back to legacy outcome statistics", async () => {
    const calls: string[] = [];
    globalThis.fetch = (async (path: unknown) => {
      calls.push(String(path));
      return new Response("Unavailable", { status: 503 });
    }) as typeof fetch;
    const { host } = await render(
      <SessionDetail identity={identityOf(session)} />,
    );
    await waitFor(() =>
      host.textContent?.includes("Could not read session analysis"),
    );
    expect(calls.some((path) => path.includes("/stats?"))).toBe(false);
    expect(host.querySelector('a[href*="/stats/export"]') === null).toBe(true);
  });
  test("a gated campaign without exact current analysis never requests old statistics", async () => {
    const calls: string[] = [];
    globalThis.fetch = (async (path: unknown) => {
      calls.push(String(path));
      return Response.json(
        resultFixture({
          analysis: null,
          reason: "gated_campaign_report",
          statistics_visibility: "withheld",
        }),
      );
    }) as typeof fetch;
    const { host } = await render(
      <SessionDetail identity={identityOf(session)} />,
    );
    await waitFor(() =>
      host.textContent?.includes("passed its disclosure gate"),
    );
    expect(calls.some((path) => path.includes("/stats?"))).toBe(false);
    expect(host.querySelector('a[href*="/stats/export"]') === null).toBe(true);
  });
  test("trusted nonblind active legacy keeps live statistics while current analysis remains withheld", async () => {
    current = [
      {
        ...session,
        state: "running",
        last_episode: { outcome: "success", steps: 8 },
      },
    ];
    const calls: string[] = [];
    globalThis.fetch = (async (path: unknown) => {
      calls.push(String(path));
      return String(path).includes("/results/session?")
        ? Response.json(
            resultFixture({
              status: "active",
              result_visibility: "withheld",
              statistics_visibility: "visible",
              analysis: null,
              records: [],
              runs: [],
              reports: [],
            }),
          )
        : Response.json({
            enabled: true,
            schema: "levi.live.stats.v1",
            source_identities: [statisticsSourceFixture()],
            scope: { dataset: "model__task", session: "run-1" },
            include_excluded: false,
            datasets: ["model__task"],
            sessions: [],
            summary: { episodes: { count: 1 } },
            episodes: {
              rows: [
                {
                  dataset: "model__task",
                  session: "run-1",
                  demo: "trusted-active-legacy",
                  state: "done",
                },
              ],
              total: 1,
            },
          });
    }) as typeof fetch;
    const { host } = await render(
      <SessionDetail identity={identityOf(session)} />,
    );
    await waitFor(() => host.textContent?.includes("trusted-active-legacy"));
    expect(calls.some((path) => path.includes("/stats?"))).toBe(true);
    expect(host.querySelector('a[href*="/stats/export"]') !== null).toBe(true);
    expect(
      host.querySelector('a[href*="/results/session/download"]') === null,
    ).toBe(true);
    expect(host.querySelector("meter") === null).toBe(true);
  });
  test("an active source with withheld or unknown statistics privacy never requests old outcomes", async () => {
    for (const visibility of ["withheld", "unknown"] as const) {
      const calls: string[] = [];
      globalThis.fetch = (async (path: unknown) => {
        calls.push(String(path));
        return Response.json(
          resultFixture({
            status: "active",
            result_visibility: "withheld",
            statistics_visibility: visibility,
            analysis: null,
          }),
        );
      }) as typeof fetch;
      const { host } = await render(
        <SessionDetail identity={identityOf(session)} />,
      );
      await waitFor(() => host.textContent?.includes("Evaluation in progress"));
      expect(calls.some((path) => path.includes("/stats?"))).toBe(false);
      expect(host.querySelector('a[href*="/stats/export"]') === null).toBe(
        true,
      );
      expect(host.textContent).not.toContain("demo-7");
    }
  });
  test("unknown statistics eligibility never falls back from visible analysis", async () => {
    const calls: string[] = [];
    globalThis.fetch = (async (path: unknown) => {
      calls.push(String(path));
      return Response.json(resultFixture({ statistics_visibility: "unknown" }));
    }) as typeof fetch;
    const { host } = await render(
      <SessionDetail identity={identityOf(session)} />,
    );
    await waitFor(() => host.textContent?.includes("Analysis available"));
    expect(calls.some((path) => path.includes("/stats?"))).toBe(false);
    expect(host.querySelector('a[href*="/stats/export"]') === null).toBe(true);
  });
  test("visible to withheld removes old statistics before a pending result refresh resolves", async () => {
    let allowed = true;
    let pending: (response: Response) => void = () => {};
    globalThis.fetch = ((path: unknown) => {
      if (String(path).includes("/results/session?"))
        return allowed
          ? Promise.resolve(Response.json(resultFixture()))
          : new Promise<Response>((r) => {
              pending = r;
            });
      return Promise.resolve(
        Response.json({
          enabled: true,
          schema: "levi.live.stats.v1",
          source_identities: [statisticsSourceFixture()],
          scope: { dataset: "model__task", session: "run-1" },
          include_excluded: false,
          datasets: ["model__task"],
          sessions: [],
          summary: { episodes: { count: 1 } },
          episodes: {
            rows: [
              {
                dataset: "model__task",
                session: "run-1",
                demo: "old-outcome-row",
                state: "done",
              },
            ],
            total: 1,
          },
        }),
      );
    }) as typeof fetch;
    const { host, rerender } = await render(
      <SessionDetail identity={identityOf(session)} />,
    );
    await waitFor(() => host.textContent?.includes("old-outcome-row"));
    allowed = false;
    lastOk = 60000;
    await rerender(<SessionDetail identity={identityOf(session)} />);
    expect(host.textContent).not.toContain("old-outcome-row");
    expect(host.querySelector('a[href*="/stats/export"]') === null).toBe(true);
    pending(
      Response.json(
        resultFixture({
          status: "blocked",
          result_visibility: "withheld",
          analysis: null,
        }),
      ),
    );
    await waitFor(() => host.textContent?.includes("Result access blocked"));
    expect(host.textContent).not.toContain("old-outcome-row");
  });
  test("invalid bookmarks are explained and have a return link", async () => {
    const { host } = await render(<SessionDetail identity={null} />);
    expect(host.textContent).toContain("Invalid session link");
    expect(host.querySelector('a[href="/live"]')).not.toBeNull();
  });
});
