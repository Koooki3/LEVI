import { click, render, setupDom } from "../../ds/__tests__/dom";
import { describe, expect, test } from "bun:test";
import { readFileSync } from "fs";
import { join } from "path";
import { DatasetFormatBadge } from "../../dataset-format";
import HfAuthButton from "../../hf-auth-button";
import type { DatasetFormat } from "@/types/dataset-format.types";

setupDom();

describe("DatasetFormatBadge (stage 4, same on every page)", () => {
  test("a raw capture is an info badge (a fact, not a warning) with its detail lines", async () => {
    const { host } = await render(
      <DatasetFormatBadge
        format={
          {
            origin: "raw_capture",
            input_format: "robot_capture",
            view_status: "ready",
            fps: 10,
          } as DatasetFormat
        }
      />,
    );
    const badge = host.querySelector(".ds-badge")!;
    expect(badge.className).toContain("ds-badge--info");
    expect(badge.className).not.toContain("ds-badge--warning");
    expect(badge.textContent).toBe("Raw capture");
    expect(host.querySelectorAll(".levi-format-line").length).toBe(2);
    expect(host.querySelector(".levi-status")).toBeNull();
  });

  test("a converted dataset is neutral; compact shows only the badge", async () => {
    const { host } = await render(
      <DatasetFormatBadge
        compact
        format={{ origin: "external", version: "v2.1" } as DatasetFormat}
      />,
    );
    expect(host.querySelector(".ds-badge")!.className).toContain(
      "ds-badge--neutral",
    );
    expect(host.textContent).toBe("LeRobot v2.1");
  });
});

describe("HfAuthButton without OAuth: the token login", () => {
  test("opens a ds dialog with a labelled password field", async () => {
    const { host } = await render(<HfAuthButton />);
    const open = host.querySelector("button")!;
    expect(open.className).toContain("ds-btn");
    expect(open.textContent).toBe("Connect Hugging Face");
    await click(open);
    const dialog = document.querySelector('[role="dialog"]')!;
    expect(dialog).not.toBeNull();
    const input = dialog.querySelector('input[type="password"]')!;
    expect(input.className).toContain("ds-input");
    expect(dialog.textContent).toContain("Hugging Face token");
  });
});

describe("no old palette in the shared components", () => {
  test.each(["hf-auth-button.tsx", "dataset-format.tsx"])("%s", (file) => {
    const code = readFileSync(join(import.meta.dir, "../..", file), "utf8")
      .replace(/\/\*[\s\S]*?\*\//g, "")
      .replace(/^\s*\/\/.*$/gm, "");
    expect(
      code.match(
        /\b(cyan|slate)-\d|levi-(primary|secondary|input|status)\b|uppercase|tracking-wide|<svg|[↗→]/g,
      ),
    ).toBeNull();
  });
  test("shared.css uses tokens only", () => {
    const css = readFileSync(
      join(import.meta.dir, "..", "shared.css"),
      "utf8",
    ).replace(/\/\*[\s\S]*?\*\//g, "");
    expect(css.match(/#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(/g)).toBeNull();
  });
});
