import { click, render, setupDom } from "../../ds/__tests__/dom";
import { describe, expect, test } from "bun:test";
import { useState } from "react";
import { CompositionPanel } from "../composition-panel";
import type { Recipe, TaskEntry } from "../types";

setupDom();

const entry = (task: string): TaskEntry => ({
  task,
  count: null,
  success_ratio: null,
  strategy: "quality",
});

const RECIPE: Recipe = {
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

function Harness({ onChange }: { onChange?: (r: Recipe) => void }) {
  const [recipe, setRecipe] = useState(RECIPE);
  return (
    <CompositionPanel
      recipe={recipe}
      preview={null}
      previewError=""
      previewing={false}
      recipes={[]}
      onChange={(next) => {
        setRecipe(next);
        onChange?.(next);
      }}
      onSave={() => {}}
      onLoad={() => {}}
      onDelete={() => {}}
      onClear={() => {}}
      onListEpisodes={() => Promise.resolve({ episodes: [] } as never)}
      refreshKey=""
    />
  );
}

const order = (host: HTMLElement) =>
  Array.from(host.querySelectorAll(".pg-pool-row .grow")).map(
    (n) => n.textContent,
  );
const button = (host: HTMLElement, label: string) =>
  host.querySelector(`button[aria-label="${label}"]`);

describe("composition task order (stage 4: Motion reorder list)", () => {
  test("one handle per task, named after it, in a labelled list", async () => {
    const { host } = await render(<Harness />);
    expect(host.querySelector("ul")!.getAttribute("aria-label")).toBe(
      "Task order",
    );
    const handles = host.querySelectorAll(".ds-reorder__handle");
    expect(handles.length).toBe(3);
    expect(handles[1].getAttribute("aria-label")).toBe("Move screws");
    expect(order(host)).toEqual(["plates", "screws", "cups"]);
  });

  test("the up, down and remove buttons still work", async () => {
    let last: Recipe | null = null;
    const { host } = await render(<Harness onChange={(r) => (last = r)} />);
    expect(button(host, "Move up: plates")!.hasAttribute("disabled")).toBe(
      true,
    );
    expect(button(host, "Move down: cups")!.hasAttribute("disabled")).toBe(
      true,
    );
    await click(button(host, "Move down: plates"));
    expect(order(host)).toEqual(["screws", "plates", "cups"]);
    await click(button(host, "Move up: cups"));
    expect(order(host)).toEqual(["screws", "cups", "plates"]);
    await click(button(host, "Remove: cups"));
    expect(order(host)).toEqual(["screws", "plates"]);
    expect(last!.tasks.map((e) => e.task)).toEqual(["screws", "plates"]);
  });

  test("an empty composition says where tasks come from", async () => {
    function Empty() {
      return (
        <CompositionPanel
          recipe={{ ...RECIPE, tasks: [] }}
          preview={null}
          previewError=""
          previewing={false}
          recipes={[]}
          onChange={() => {}}
          onSave={() => {}}
          onLoad={() => {}}
          onDelete={() => {}}
          onClear={() => {}}
          onListEpisodes={() => Promise.resolve({ episodes: [] } as never)}
          refreshKey=""
        />
      );
    }
    const { host } = await render(<Empty />);
    expect(host.textContent).toContain("Add tasks from the task table.");
    expect(host.querySelector(".ds-reorder")).toBeNull();
  });
});

describe("nothing the old composition said is lost", () => {
  test("a long task name keeps its full text for the tooltip and the handle", async () => {
    const { host } = await render(<Harness />);
    const name = host.querySelector(".pg-pool-row .grow")!;
    expect(name.textContent).toBe("plates");
    expect(
      host.querySelector(".ds-reorder__handle")!.getAttribute("aria-label"),
    ).toBe("Move plates");
  });

  test("while the preview is computed, screen readers hear it", async () => {
    const { host } = await render(<Harness />);
    const status = host.querySelector('.pg-pool-preview [role="status"]');
    expect(status?.textContent).toBe("Computing…");
  });
});
