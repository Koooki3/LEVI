import { render, setupDom, waitFor } from "@/components/ds/__tests__/dom";
import { afterEach, describe, expect, test } from "bun:test";
import { readFileSync } from "fs";
import { join } from "path";
import { DatasetFormatBadge } from "@/components/dataset-format";
import { DatasetSourceProvider } from "@/context/dataset-source-context";
import { RawCaptureNotice } from "../raw-capture-notice";
import type { DatasetFormat } from "@/types/dataset-format.types";

// The real source context, answered by a catalog entry (a module mock of it
// would stay in force for every other test file of the run).
const entry = {
  id: "local/task_a",
  name: "task_a",
  path: "/data/task_a",
  format: {
    kind: "raw",
    origin: "raw_capture",
    input_format: "robot_capture",
    view_status: "ready",
  },
};
const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});
function Shown({ compact }: { compact: boolean }) {
  return (
    <DatasetSourceProvider org="local" dataset="task_a">
      <RawCaptureNotice compact={compact} />
    </DatasetSourceProvider>
  );
}

setupDom();

describe("a raw capture is a fact, not a warning", () => {
  test("the notice is an info note with an info badge, in both sizes", async () => {
    globalThis.fetch = (() =>
      Promise.resolve(
        new Response(JSON.stringify(entry), {
          headers: { "Content-Type": "application/json" },
        }),
      )) as unknown as typeof fetch;
    for (const compact of [false, true]) {
      const { host } = await render(<Shown compact={compact} />);
      const note = await waitFor(() => host.querySelector(".vw-note"));
      expect(note.getAttribute("role")).toBe("note");
      expect(note.className).not.toContain("vw-note--danger");
      const badge = note.querySelector(".ds-badge")!;
      expect(badge.className).toContain("ds-badge--info");
      expect(badge.className).not.toContain("ds-badge--warning");
      expect(note.querySelector("a")?.getAttribute("href")).toBe(
        "/workbench?source=%2Fdata%2Ftask_a",
      );
    }
  });

  test("the note's bar and icon are the info colour in the stylesheet", () => {
    const css = readFileSync(
      join(import.meta.dir, "../viewer/viewer.css"),
      "utf8",
    );
    expect(css).toMatch(
      /\.vw-note \{[^}]*border-left:\s*3px solid var\(--ds-info\)/,
    );
    expect(css).toMatch(
      /\.vw-note-head > \.ds-icon \{[^}]*color:\s*var\(--ds-info\)/,
    );
    expect(css).not.toMatch(/\.vw-note \{[^}]*--ds-warning/);
  });

  test("only a failed browsing view is danger", async () => {
    const { host } = await render(
      <DatasetFormatBadge
        compact
        format={
          {
            origin: "raw_capture",
            input_format: "robot_capture",
            view_status: "failed",
          } as DatasetFormat
        }
      />,
    );
    expect(host.querySelector(".ds-badge")!.className).toContain(
      "ds-badge--danger",
    );
  });
});
