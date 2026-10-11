import { afterEach, expect, test } from "bun:test";
import {
  click,
  flush,
  render,
  setupDom,
  waitFor,
} from "@/components/ds/__tests__/dom";
import { SessionStatisticsExport } from "../session-statistics-export";
import { STATS_BINDING_SCHEMA, exportHref } from "../stats-logic";
import { statisticsSourceFixture } from "./statistics-fixture";
setupDom();
const fetchBefore = globalThis.fetch;
const createBefore = URL.createObjectURL;
const revokeBefore = URL.revokeObjectURL;
const clickBefore = HTMLAnchorElement.prototype.click;
let saved = 0;
function saveProbe() {
  URL.createObjectURL = () => "blob:verified-download";
  URL.revokeObjectURL = () => {};
  HTMLAnchorElement.prototype.click = function () {
    if (this.href.startsWith("blob:")) saved++;
    else clickBefore.call(this);
  };
}
afterEach(() => {
  globalThis.fetch = fetchBefore;
  URL.createObjectURL = createBefore;
  URL.revokeObjectURL = revokeBefore;
  HTMLAnchorElement.prototype.click = clickBefore;
  saved = 0;
});
const scope = {
  dataset: "model__task",
  session: "run-1",
  includeExcluded: false,
};
const sources = [statisticsSourceFixture()];
function success() {
  return new Response("authorized A report", {
    headers: { "X-LEVI-Stats-Source-Binding": STATS_BINDING_SCHEMA },
  });
}

test("all three session export links bind the verified full identity while generic links remain compatible", () => {
  for (const format of ["md", "json", "csv"] as const) {
    const href = exportHref(format, scope, "zh", sources);
    const params = new URL(href, "http://localhost").searchParams;
    expect(JSON.parse(params.get("expected_sources")!)).toEqual({
      schema: STATS_BINDING_SCHEMA,
      identities: sources,
    });
    expect(params.get("format")).toBe(format);
    expect(params.get("lang")).toBe("zh");
  }
  expect(
    new URL(
      exportHref("csv", scope, "en"),
      "http://localhost",
    ).searchParams.has("expected_sources"),
  ).toBe(false);
});
for (const format of ["Markdown", "JSON", "CSV"]) {
  test(`saves ${format} only after server source binding acknowledgement`, async () => {
    saveProbe();
    let asked = "";
    globalThis.fetch = (async (path: unknown) => {
      asked = String(path);
      return success();
    }) as typeof fetch;
    const { host } = await render(
      <SessionStatisticsExport
        scope={scope}
        sources={sources}
        onError={() => {
          throw new Error("unexpected failure");
        }}
      />,
    );
    await click(
      [...host.querySelectorAll("a")].find((a) => a.textContent === format)!,
    );
    await waitFor(() => saved === 1);
    expect(asked).toContain("expected_sources=");
    expect(host.querySelector("p")?.getAttribute("aria-busy")).toBe("false");
  });
}
for (const status of [409, 503, 200]) {
  test(`status ${status} without binding acknowledgement never saves and gives clear feedback`, async () => {
    saveProbe();
    let error = "";
    globalThis.fetch = (async () =>
      new Response("foreign/private report", { status })) as typeof fetch;
    const { host } = await render(
      <SessionStatisticsExport
        scope={scope}
        sources={sources}
        onError={(message) => {
          error = message;
        }}
      />,
    );
    await click(host.querySelector("a"));
    await waitFor(() => error);
    expect(saved).toBe(0);
    expect(error).toContain(
      status === 503 ? "Could not download" : "Statistics source changed",
    );
  });
}
test("a late old-source success or error cannot save or affect a new source download", async () => {
  for (const status of [200, 409]) {
    saveProbe();
    let resolve: (response: Response) => void = () => {};
    let count = 0;
    let errors = 0;
    globalThis.fetch = (() =>
      ++count === 1
        ? new Promise<Response>((r) => {
            resolve = r;
          })
        : Promise.resolve(success())) as typeof fetch;
    const { host, rerender } = await render(
      <SessionStatisticsExport
        scope={scope}
        sources={sources}
        onError={() => {
          errors++;
        }}
      />,
    );
    await click(host.querySelector("a"));
    const rebound = [statisticsSourceFixture({ root: "/new-authorized-root" })];
    await rerender(
      <SessionStatisticsExport
        scope={scope}
        sources={rebound}
        onError={() => {
          errors++;
        }}
      />,
    );
    await click(host.querySelector("a"));
    await waitFor(() => saved > 0);
    const before = saved;
    resolve(status === 200 ? success() : new Response("obsolete", { status }));
    await flush();
    expect(saved).toBe(before);
    expect(errors).toBe(0);
  }
});
test("unmount while export is pending revokes the request and prevents a late save", async () => {
  saveProbe();
  let resolve: (response: Response) => void = () => {};
  let error = "";
  globalThis.fetch = (() =>
    new Promise<Response>((r) => {
      resolve = r;
    })) as typeof fetch;
  const { host, rerender } = await render(
    <SessionStatisticsExport
      scope={scope}
      sources={sources}
      onError={(message) => {
        error = message;
      }}
    />,
  );
  await click(host.querySelector("a"));
  await rerender(<div>Statistics revoked</div>);
  resolve(success());
  await flush();
  expect(saved).toBe(0);
  expect(error).toBe("");
});
