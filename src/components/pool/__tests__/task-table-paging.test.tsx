import { click, render, setupDom } from "@/components/ds/__tests__/dom";
import { describe, expect, mock, test } from "bun:test";

mock.module("next/link", () => ({
  default: ({ children, ...props }: Record<string, unknown>) => (
    <a {...(props as object)}>{children as never}</a>
  ),
}));
const { TaskTable } = await import("../pool-tables");
import type { TaskRow } from "../types";

setupDom();

const row = (i: number): TaskRow => ({
  task: `task ${String(i).padStart(3, "0")}`,
  spellings: [`task ${i}`],
  episodes: 10,
  frames: 1000,
  categories: { rollout: 10 },
  success: 5,
  failure: 5,
  success_rate: 0.5,
  sources: ["s"],
});
const many = Array.from({ length: 150 }, (_, i) => row(i));
const props = {
  chosen: [] as string[],
  suggest: async () => {
    throw new Error("not used");
  },
  onAdd: () => {},
  onFocus: () => {},
};
const rows = (host: HTMLElement) => host.querySelectorAll("tbody tr");
const button = (host: HTMLElement, text: string) =>
  Array.from(host.querySelectorAll("button")).find((b) =>
    b.textContent?.includes(text),
  );

describe("the pool's task table", () => {
  test("a long list draws one page, says how many there are, and shows all on request", async () => {
    const { host } = await render(
      <TaskTable tasks={many} focus={null} {...props} />,
    );
    expect(rows(host)).toHaveLength(60);
    expect(host.textContent).toContain("Showing 60 of 150");
    await click(button(host, "Show all 150"));
    expect(rows(host)).toHaveLength(150);
    await click(button(host, "Show fewer"));
    expect(rows(host)).toHaveLength(60);
  });

  test("the focused task stays drawn even when it is past the page", async () => {
    const { host } = await render(
      <TaskTable tasks={many} focus="task 120" {...props} />,
    );
    expect(rows(host)).toHaveLength(61);
    expect(host.textContent).toContain("task 120");
    expect(host.textContent).not.toContain("task 119");
  });

  test("a short list has no paging controls", async () => {
    const { host } = await render(
      <TaskTable tasks={many.slice(0, 20)} focus={null} {...props} />,
    );
    expect(rows(host)).toHaveLength(20);
    expect(host.textContent).not.toContain("Show all");
  });
});
