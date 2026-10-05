import { render, setupDom } from "@/components/ds/__tests__/dom";
import { describe, expect, mock, test } from "bun:test";
import { readFileSync } from "fs";
import { join } from "path";
import { DatasetFormatBadge } from "@/components/dataset-format";
import type { DatasetFormat } from "@/types/dataset-format.types";

const source = {
  isRaw: true,
  entry: { path: "/data/task_a" },
  format: {
    origin: "raw_capture",
    input_format: "robot_capture",
    view_status: "ready",
  },
};
mock.module("@/context/dataset-source-context", () => ({
  useDatasetSource: () => source,
}));
const { RawCaptureNotice } = await import("../raw-capture-notice");

setupDom();

describe("a raw capture is a fact, not a warning", () => {
  test("the notice is an info note with an info badge, in both sizes", async () => {
    for (const compact of [false, true]) {
      const { host } = await render(<RawCaptureNotice compact={compact} />);
      const note = host.querySelector(".vw-note")!;
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
