import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import { ServicePanel } from "../service-panel";
import type { LiveStatusResponse } from "../types";

const status = {
  service: { state: "stopped", gpu: {}, resources: {} },
} as unknown as LiveStatusResponse;

describe("the service panel of a service that is not running", () => {
  const html = renderToStaticMarkup(
    <ServicePanel status={status} alive={false} now={Date.now() / 1000} />,
  );

  test("says so in words and is not faded", () => {
    expect(html).toContain("Not running");
    // `.pg-live-section.stale` set opacity 0.8, which took chips and labels
    // under 4.5:1; the state is in words and the section keeps its contrast.
    expect(html).not.toMatch(/class="[^"]*\bstale\b/);
    expect(html).toContain('data-alive="false"');
  });
});

describe("the service panel with no status file at all", () => {
  test("is an empty line in words, with no fade and no service state", () => {
    const html = renderToStaticMarkup(
      <ServicePanel status={null} alive={false} now={0} />,
    );
    expect(html).toContain("No status file yet.");
    expect(html).not.toMatch(/class="[^"]*\bstale\b/);
    expect(html).not.toContain("Not running");
  });
});
