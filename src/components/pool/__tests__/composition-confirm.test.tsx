import { click, fire, render, setupDom, waitFor } from "../../ds/__tests__/dom";
import { describe, expect, mock, test } from "bun:test";

mock.module("next/navigation", () => ({
  useRouter: () => ({ push: () => {}, replace: () => {} }),
  usePathname: () => "/pool",
}));

const { ConfirmProvider } = await import("@/components/shell/confirm");
const { CompositionPanel } = await import("../composition-panel");
type Recipe = import("../types").Recipe;

setupDom();

const entry = (task: string) => ({
  task,
  count: null,
  success_ratio: null,
  strategy: "quality" as const,
});

const BASE: Recipe = {
  name: "",
  categories: [],
  sources: [],
  tasks: [],
  outcome: "all",
  per_task_cap: null,
  seed: 0,
  date_from: null,
  date_to: null,
  policies: [],
  exclude: [],
  include_nonstandard: false,
};

type Calls = { save: number; load: string[]; clear: number };

function Panel({
  recipe,
  recipes = [],
  calls,
}: {
  recipe: Recipe;
  recipes?: Recipe[];
  calls: Calls;
}) {
  return (
    <ConfirmProvider>
      <CompositionPanel
        recipe={recipe}
        preview={null}
        previewError=""
        previewing={false}
        recipes={recipes}
        onChange={() => {}}
        onSave={() => (calls.save += 1)}
        onLoad={(name) => calls.load.push(name)}
        onDelete={() => {}}
        onClear={() => (calls.clear += 1)}
        onListEpisodes={() => Promise.resolve({ episodes: [] } as never)}
        refreshKey=""
      />
    </ConfirmProvider>
  );
}

const fresh = (): Calls => ({ save: 0, load: [], clear: 0 });
const byText = (host: Element, label: string) =>
  [...host.querySelectorAll("button")].find((b) => b.textContent === label);
const dialog = (host: Element) => host.querySelector("[role=alertdialog]");

describe("composition actions that discard work ask first", () => {
  test("Start a new composition: asks when there are tasks, answers no, then yes", async () => {
    const calls = fresh();
    const { host } = await render(
      <Panel recipe={{ ...BASE, tasks: [entry("a")] }} calls={calls} />,
    );
    await click(byText(host, "Start a new composition")!);
    const box = await waitFor(() => dialog(host), { label: "the question" });
    expect(calls.clear).toBe(0);
    await click(byText(box, "Cancel")!);
    await waitFor(() => !dialog(host), { label: "the question closed" });
    expect(calls.clear).toBe(0);
    await click(byText(host, "Start a new composition")!);
    const again = await waitFor(() => dialog(host), { label: "asked again" });
    await click(byText(again, "Start a new composition")!);
    await waitFor(() => calls.clear === 1, { label: "the clear" });
  });

  test("nothing to lose: an empty composition starts a new one without a question", async () => {
    const calls = fresh();
    const { host } = await render(<Panel recipe={BASE} calls={calls} />);
    await click(byText(host, "Start a new composition")!);
    expect(dialog(host)).toBeNull();
    expect(calls.clear).toBe(1);
  });

  test("a composition equal to its saved recipe is not unsaved work", async () => {
    const calls = fresh();
    const saved = { ...BASE, name: "mix", tasks: [entry("a")] };
    const { host } = await render(
      <Panel recipe={saved} recipes={[saved]} calls={calls} />,
    );
    await click(byText(host, "Start a new composition")!);
    expect(dialog(host)).toBeNull();
    expect(calls.clear).toBe(1);
  });

  test("Load asks before replacing unsaved work", async () => {
    const calls = fresh();
    const other = { ...BASE, name: "other", tasks: [entry("z")] };
    const { host } = await render(
      <Panel
        recipe={{ ...BASE, name: "mine", tasks: [entry("a")] }}
        recipes={[other]}
        calls={calls}
      />,
    );
    const select = host.querySelector("select")!;
    select.value = "other";
    await fire(select, new Event("change", { bubbles: true }));
    await click(byText(host, "Load")!);
    const box = await waitFor(() => dialog(host), { label: "the question" });
    expect(box.textContent).toContain("other");
    expect(calls.load).toEqual([]);
    await click(byText(box, "Load")!);
    await waitFor(() => calls.load.length === 1, { label: "the load" });
    expect(calls.load).toEqual(["other"]);
  });

  test("Save over a recipe of the same name asks; a new name saves at once", async () => {
    const calls = fresh();
    const existing = { ...BASE, name: "mix", tasks: [entry("old")] };
    const { host, rerender } = await render(
      <Panel
        recipe={{ ...BASE, name: "mix", tasks: [entry("a")] }}
        recipes={[existing]}
        calls={calls}
      />,
    );
    await click(byText(host, "Save")!);
    const box = await waitFor(() => dialog(host), { label: "the question" });
    expect(box.textContent).toContain("mix");
    expect(calls.save).toBe(0);
    await click(byText(box, "Overwrite")!);
    await waitFor(() => calls.save === 1, { label: "the save" });
    await rerender(
      <Panel
        recipe={{ ...BASE, name: "fresh", tasks: [entry("a")] }}
        recipes={[existing]}
        calls={calls}
      />,
    );
    await click(byText(host, "Save")!);
    expect(dialog(host)).toBeNull();
    expect(calls.save).toBe(2);
  });

  test("Save is off with no tasks, and the reason is in words", async () => {
    const calls = fresh();
    const { host } = await render(
      <Panel recipe={{ ...BASE, name: "mix" }} calls={calls} />,
    );
    const save = byText(host, "Save")!;
    expect(save.hasAttribute("disabled")).toBe(true);
    const why = host.querySelector(
      `#${save.getAttribute("aria-describedby")}`,
    )!;
    expect(why.textContent).toContain("Add at least one task");
  });
});
