import { flush, render, setupDom } from "@/components/ds/__tests__/dom";
import { describe, expect, test } from "bun:test";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";

const { DataLoadNotice } = await import("../data-notice");

setupDom();

const MESSAGE = "Rows 0-10 of x needs about 300 MiB, above the 128 MiB limit.";

describe("the notice for data above the memory limit", () => {
  test("is an alert that says the charts are empty for size, with the numbers under details", async () => {
    const { host } = await render(<DataLoadNotice message={MESSAGE} />);
    await flush();
    const alert = host.querySelector("[role=alert]");
    expect(alert).toBeTruthy();
    expect(alert?.textContent).toContain("too large to chart");
    expect(alert?.textContent).toContain("charts are empty");
    expect(host.querySelector("details pre")?.textContent).toBe(MESSAGE);
  });

  test("for the analysis it names how many episodes were left out", async () => {
    const { host } = await render(
      <DataLoadNotice message={MESSAGE} skippedEpisodes={7} />,
    );
    await flush();
    expect(host.textContent).toContain("left out of this analysis");
    expect(host.textContent).toContain("7");
  });

  test("its texts exist in both language catalogues", () => {
    for (const key of [
      "This episode's data is too large to chart",
      "Some episodes were left out of this analysis",
      "Episodes left out because their data file is too large:",
    ]) {
      expect(key in en).toBe(true);
      expect((zh as Record<string, string>)[key]).toBeTruthy();
    }
  });
});
