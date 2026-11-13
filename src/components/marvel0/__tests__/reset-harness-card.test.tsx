import {
  click,
  fire,
  render,
  setupDom,
  waitFor,
} from "@/components/ds/__tests__/dom";
import { describe, expect, test } from "bun:test";
import { LocaleProvider } from "@/components/levi-locale";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import { ResetHarnessCard } from "../reset-harness-card";
import { parseResetHarnessRun } from "../reset-harness-doc";
import wire from "./reset-harness-wire.samples.json";
import {
  completedReset,
  fakeResetApi,
  queuedReset,
  resetCatalog,
} from "./reset-harness-fixtures";

setupDom();
const get = (host: HTMLElement, id: string) =>
  host.querySelector(`[data-testid="reset-harness-${id}"]`);
async function open(f = fakeResetApi()) {
  const view = await render(<ResetHarnessCard api={f.api} pollMs={25} />);
  await click(get(view.host, "toggle"));
  await waitFor(() => !get(view.host, "start")?.hasAttribute("disabled"));
  return { ...view, ...f };
}

describe("offline rehearsal inside autonomy", () => {
  test("shows final release from an actual worker result separately from earlier holding", async () => {
    const run = parseResetHarnessRun(wire.success)!;
    const f = await open(fakeResetApi([run]));
    await waitFor(() => get(f.host, "events"));
    expect(get(f.host, "holding")?.textContent).toBe(
      en["resetHarness.holding.pass"],
    );
    expect(get(f.host, "final")?.textContent).toBe(
      en["resetHarness.final.pass"],
    );
    expect(f.host.textContent).toContain("synthetic_final_reset_verified");
    expect(f.host.textContent).toContain('"released": true');
    expect(f.host.textContent).toContain('"object_at_target": true');
    expect(get(f.host, "events")?.querySelectorAll("li")).toHaveLength(
      run.events.length,
    );
  });
  test("is collapsed initially and reads or writes nothing before it is opened", async () => {
    const f = fakeResetApi();
    const { host } = await render(<ResetHarnessCard api={f.api} />);
    expect(get(host, "toggle")?.getAttribute("aria-expanded")).toBe("false");
    expect(f.state.reads).toEqual([]);
    expect(f.state.writes).toEqual([]);
    expect(get(host, "start")).toBeNull();
    await click(get(host, "toggle"));
    await waitFor(() => f.state.reads.length > 0);
    expect(f.state.writes).toEqual([]);
    expect(host.textContent).toContain(en["resetHarness.boundary"]);
  });
  test("starts only after a click and shows no invented completed steps", async () => {
    const f = await open();
    expect(f.state.writes).toEqual([]);
    await click(get(f.host, "start"));
    await waitFor(() => get(f.host, "run"));
    expect(f.state.writes).toHaveLength(1);
    expect(f.state.writes[0]).toMatchObject({
      kind: "start",
      body: { scenario_id: "normal_random", seed: 0 },
    });
    expect(f.host.textContent).toContain(en["resetHarness.noEvents"]);
    expect(f.host.textContent).not.toContain(
      en["resetHarness.status.succeeded"],
    );
    expect(get(f.host, "holding")?.textContent).toBe(
      en["resetHarness.holding.unknown"],
    );
    expect(get(f.host, "final")?.textContent).toBe(
      en["resetHarness.final.unknown"],
    );
    expect(get(f.host, "events")).toBeNull();
  });
  test("stops only the selected offline job", async () => {
    const f = await open(fakeResetApi([queuedReset()]));
    await waitFor(() => get(f.host, "stop"));
    await click(get(f.host, "stop"));
    await waitFor(() => !get(f.host, "stop"));
    expect(f.state.writes).toHaveLength(1);
    expect(f.state.writes[0]).toMatchObject({
      kind: "stop",
      id: "offline-run-1",
    });
    expect(f.host.textContent).toContain(en["resetHarness.status.stopped"]);
  });
  test("shows recorded target, pre-handoff holding, final release and unavailable token source", async () => {
    const f = await open(fakeResetApi([completedReset()]));
    await waitFor(() => get(f.host, "events"));
    expect(get(f.host, "events")?.querySelectorAll("li")).toHaveLength(9);
    expect(f.host.textContent).toContain("0.4200, -0.2100, 0.1000");
    expect(f.host.textContent).toContain(en["resetHarness.holding"]);
    expect(get(f.host, "final")?.textContent).toBe(
      en["resetHarness.final.pass"],
    );
    expect(f.host.textContent).toContain(en["resetHarness.tokensUnknown"]);
    expect(f.host.textContent).toContain(en["resetHarness.physicalDenied"]);
    expect(f.state.writes).toHaveLength(0);
  });
  test("switches history without retaining the previous run's success evidence", async () => {
    const f = await open(fakeResetApi([completedReset(), queuedReset()]));
    await waitFor(() => get(f.host, "events"));
    const select = get(f.host, "history") as HTMLSelectElement;
    select.value = "offline-run-1";
    await fire(select, new Event("change", { bubbles: true }));
    await waitFor(
      () =>
        get(f.host, "holding")?.textContent ===
        en["resetHarness.holding.unknown"],
    );
    expect(get(f.host, "events")).toBeNull();
    expect(get(f.host, "handoff")?.textContent).toBe(
      en["resetHarness.handoffBlocked"],
    );
  });
  test("rejects a mixed physical response without enabling a start or showing success", async () => {
    const f = fakeResetApi();
    f.api.catalog = async () => ({
      ok: true,
      status: 200,
      data: { ...resetCatalog([completedReset()]), physical_execution: true },
    });
    const { host } = await render(<ResetHarnessCard api={f.api} pollMs={25} />);
    await click(get(host, "toggle"));
    await waitFor(() =>
      host.textContent?.includes(en["resetHarness.catalogUnavailable"]),
    );
    expect(get(host, "start")?.hasAttribute("disabled")).toBe(true);
    expect(get(host, "run")).toBeNull();
    expect(f.state.writes).toHaveLength(0);
  });
  test("retains the start request ID after an unanswered write", async () => {
    const f = fakeResetApi();
    const original = f.api.start;
    const ids: string[] = [];
    f.api.start = async (body) => {
      ids.push(body.request_id);
      if (ids.length === 1)
        return {
          ok: false,
          status: 0,
          code: "network",
          message: "unanswered",
          extra: {},
          field: null,
          fields: [],
        };
      return original(body);
    };
    const { host } = await open(f);
    await click(get(host, "start"));
    await waitFor(() =>
      get(host, "start")?.textContent?.includes(en["resetHarness.retryStart"]),
    );
    expect(ids).toHaveLength(1);
    await click(get(host, "start"));
    await waitFor(() => get(host, "run"));
    expect(ids).toHaveLength(2);
    expect(ids[0]).toBe(ids[1]);
  });
  test("renders diagnostic failure stages and lists damaged records without hiding good ones", async () => {
    const run = queuedReset();
    run.status = "failed";
    run.stage = "worker_lost";
    run.stage_seq = 1;
    run.events = [
      {
        seq: 1,
        stage: "worker_lost",
        status: "failed",
        reason: "worker_lost",
        elapsed_ms: 1,
        evidence: {},
      },
    ];
    const f = fakeResetApi([run]);
    f.api.catalog = async () => ({
      ok: true,
      status: 200,
      data: {
        ...resetCatalog([run]),
        issues: [{ run_id: "bad-record", code: "invalid_json" }],
      },
    });
    const { host } = await open(f);
    await waitFor(() => get(host, "events"));
    expect(host.textContent).toContain("worker_lost");
    expect(host.textContent).toContain("bad-record: invalid_json");
    expect(host.textContent).toContain(en["resetHarness.catalogIssues"]);
  });
  test("renders the same separation and evidence labels in Chinese", async () => {
    localStorage.setItem("levi-language", "zh");
    const f = fakeResetApi([completedReset()]);
    const { host } = await render(
      <LocaleProvider>
        <ResetHarnessCard api={f.api} pollMs={25} />
      </LocaleProvider>,
    );
    await click(get(host, "toggle"));
    await waitFor(() => get(host, "events"));
    expect(host.textContent).toContain(zh["resetHarness.boundary"]);
    expect(host.textContent).toContain(zh["resetHarness.final"]);
    expect(host.textContent).not.toContain("resetHarness.");
    localStorage.clear();
  });
});
