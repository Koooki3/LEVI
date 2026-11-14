import { click, render, setupDom, waitFor } from "../../ds/__tests__/dom";
import { afterEach, describe, expect, mock, test } from "bun:test";
import { useState } from "react";

mock.module("next/link", () => ({
  default: ({ children, ...props }: Record<string, unknown>) => (
    <a {...(props as object)}>{children as never}</a>
  ),
}));

const { EpisodeTable, TaskTable } = await import("../pool-tables");
const { CompositionPanel } = await import("../composition-panel");
const { RecentJobs } = await import("../job-list");
type Recipe = import("../types").Recipe;
type PoolJob = import("../types").PoolJob;
type TaskRow = import("../types").TaskRow;
type TaskEntry = import("../types").TaskEntry;

setupDom();

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});

const active = () => document.activeElement as HTMLElement | null;
const label = (el: Element | null) =>
  el?.getAttribute("aria-label") ?? el?.textContent ?? "";
const byText = (host: Element, text: string) =>
  [...host.querySelectorAll("button")].find((b) => b.textContent === text) as
    | HTMLButtonElement
    | undefined;
const byLabel = (host: Element, text: string) =>
  host.querySelector<HTMLButtonElement>(`button[aria-label="${text}"]`)!;

const taskRow = (task: string): TaskRow => ({
  task,
  spellings: [task],
  episodes: 200,
  frames: 1000,
  categories: { rollout: 200 },
  success: 100,
  failure: 100,
  success_rate: 0.5,
  sources: ["s"],
});

function Tasks() {
  const [chosen, setChosen] = useState<string[]>([]);
  return (
    <TaskTable
      tasks={[taskRow("plates"), taskRow("cups")]}
      chosen={chosen}
      focus={null}
      onFocus={() => {}}
      suggest={async (task) => ({
        task,
        available: 200,
        successes: 100,
        failures: 100,
        unknown: 0,
        already_used: 0,
        suggested_count: 50,
        earlier_counts: [],
      })}
      onAdd={(entry: TaskEntry) => setChosen((c) => [...c, entry.task])}
    />
  );
}

describe("focus after the task chooser closes", () => {
  test("adding leaves focus on the task, not on a button that just went off", async () => {
    const { host } = await render(<Tasks />);
    await click(byLabel(host, "Add to composition: plates"));
    const chooser = await waitFor(
      () => host.querySelector(".pg-pool-chooser"),
      {
        label: "the chooser",
      },
    );
    await click(byText(chooser, "Add to composition")!);
    expect(host.querySelector(".pg-pool-chooser")).toBeNull();
    expect(byLabel(host, "Add to composition: plates").disabled).toBe(true);
    await waitFor(() => active()?.className.includes("pg-pool-task"), {
      label: "focus on the task name",
    });
    expect(active()!.textContent).toBe("plates");
  });

  test("cancelling the chooser returns focus to that row's Add button", async () => {
    const { host } = await render(<Tasks />);
    await click(byLabel(host, "Add to composition: cups"));
    const chooser = await waitFor(
      () => host.querySelector(".pg-pool-chooser"),
      {
        label: "the chooser",
      },
    );
    await click(byText(chooser, "Cancel")!);
    await waitFor(() => !host.querySelector(".pg-pool-chooser"), {
      label: "the chooser closed",
    });
    await waitFor(() => label(active()) === "Add to composition: cups", {
      label: "focus on the Add button",
    });
  });
});

describe("focus at the end of the episode pages", () => {
  function Pages() {
    const [offset, setOffset] = useState(0);
    return (
      <EpisodeTable
        rows={[]}
        total={120}
        offset={offset}
        pageSize={50}
        chosenTasks={[]}
        exclude={[]}
        onPage={setOffset}
        onToggleExclude={() => {}}
      />
    );
  }
  test("Next page, now off, hands focus to the page range", async () => {
    const { host } = await render(<Pages />);
    await click(byText(host, "Next page")!);
    expect(byText(host, "Next page")!.disabled).toBe(false);
    await click(byText(host, "Next page")!);
    expect(byText(host, "Next page")!.disabled).toBe(true);
    expect(active()!.getAttribute("role")).toBe("status");
    expect(active()!.textContent).toContain("101");
    await click(byText(host, "Previous page")!);
    await click(byText(host, "Previous page")!);
    expect(byText(host, "Previous page")!.disabled).toBe(true);
    expect(active()!.getAttribute("role")).toBe("status");
  });
});

describe("focus in the composition", () => {
  const entry = (task: string): TaskEntry => ({
    task,
    count: null,
    success_ratio: null,
    strategy: "quality",
  });
  const BASE: Recipe = {
    name: "",
    categories: [],
    sources: [],
    tasks: [entry("plates"), entry("screws"), entry("cups")],
    outcome: "all",
    per_task_cap: null,
    seed: 0,
    date_from: null,
    date_to: null,
    policies: [],
    exclude: [],
    include_nonstandard: false,
  };
  function Composition() {
    const [recipe, setRecipe] = useState(BASE);
    return (
      <CompositionPanel
        recipe={recipe}
        preview={null}
        previewError=""
        previewing={false}
        recipes={[]}
        onChange={setRecipe}
        onSave={() => {}}
        onLoad={() => {}}
        onDelete={() => {}}
        onClear={() => {}}
        onListEpisodes={() => Promise.resolve({ episodes: [] } as never)}
        refreshKey=""
      />
    );
  }

  test("removing a task focuses the next one's Remove, then the previous, then the heading", async () => {
    const { host } = await render(<Composition />);
    await click(byLabel(host, "Remove: screws"));
    await waitFor(() => label(active()) === "Remove: cups", {
      label: "focus on the next row",
    });
    await click(byLabel(host, "Remove: cups"));
    await waitFor(() => label(active()) === "Remove: plates", {
      label: "focus on the previous row",
    });
    await click(byLabel(host, "Remove: plates"));
    await waitFor(() => active()?.id === "pool-composition", {
      label: "focus on the heading",
    });
  });

  test("moving a task to an end hands focus to the other arrow", async () => {
    const { host } = await render(<Composition />);
    await click(byLabel(host, "Move up: screws"));
    expect(byLabel(host, "Move up: screws").disabled).toBe(true);
    await waitFor(() => label(active()) === "Move down: screws", {
      label: "focus on Move down",
    });
  });
});

describe("focus in the job list", () => {
  const job = (id: string): PoolJob =>
    ({ id, kind: "export", status: "failed" }) as PoolJob;
  function Jobs() {
    const [jobs, setJobs] = useState([job("job-a"), job("job-b")]);
    globalThis.fetch = mock(() => {
      return Promise.resolve(
        new Response(JSON.stringify({ errors: [], freed_bytes: 0 }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    }) as unknown as typeof fetch;
    return (
      <RecentJobs
        jobs={jobs}
        // The service answered the delete; the page then reads the list again.
        onChanged={() => setJobs([job("job-b")])}
        onPush={() => {}}
        onJob={() => {}}
        onLog={() => {}}
        onNotice={() => {}}
      />
    );
  }
  test("clearing a record leaves focus on the next job, not on the page top", async () => {
    const { host } = await render(<Jobs />);
    const first = host.querySelector("tbody tr")!;
    await click(byText(first, "Clear record")!);
    await waitFor(() => label(active()) === "Select job-b", {
      label: "focus on the neighbouring job",
    });
  });
});
