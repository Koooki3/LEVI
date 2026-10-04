import { setupDom, click, focus, mockMatchMedia, press, render } from "./dom";
import { describe, expect, mock, test } from "bun:test";
import { useState } from "react";
import { Database } from "lucide-react";
import {
  Badge,
  Card,
  Divider,
  EmptyState,
  Kbd,
  Skeleton,
  SkeletonText,
  StatusDot,
  Tag,
} from "../Display";
import { Progress, Spinner } from "../Progress";
import { SegmentedControl, Tabs, selectedIndex } from "../Tabs";
import { ReducedMotionScope } from "@/lib/design/motion";
import { Table, TableRow } from "../Table";
import { ThemePicker } from "../ThemePicker";

setupDom();

describe("Badge, Tag, StatusDot", () => {
  test("status is icon + words, not colour alone", async () => {
    const { host } = await render(
      <>
        <Badge tone="danger">Failed</Badge>
        <StatusDot tone="success">Done</StatusDot>
      </>,
    );
    const badge = host.querySelector(".ds-badge")!;
    expect(badge.className).toContain("ds-badge--danger");
    expect(badge.querySelector("svg")).not.toBeNull();
    expect(badge.textContent).toBe("Failed");
    const dot = host.querySelector(".ds-status")!;
    expect(dot.querySelector("svg")!.getAttribute("aria-hidden")).toBe("true");
    expect(dot.textContent).toBe("Done");
  });

  test("tone icons differ in shape", async () => {
    const { host } = await render(
      <>
        {(["neutral", "success", "warning", "danger", "info"] as const).map(
          (tone) => (
            <Badge key={tone} tone={tone}>
              {tone}
            </Badge>
          ),
        )}
      </>,
    );
    const classes = Array.from(host.querySelectorAll("svg")).map(
      (svg) =>
        svg
          .getAttribute("class")!
          .split(" ")
          .find((c) => c.startsWith("lucide-"))!,
    );
    expect(new Set(classes).size).toBe(5);
  });

  test("a live status breathes; reduced motion is handled in CSS", async () => {
    const { host } = await render(
      <StatusDot tone="success" live>
        Running
      </StatusDot>,
    );
    expect(host.querySelector(".ds-breathe")).not.toBeNull();
  });

  test("a removable tag names its remove button", async () => {
    const onRemove = mock(() => undefined);
    const { host } = await render(<Tag onRemove={onRemove}>grasp</Tag>);
    const remove = host.querySelector("button")!;
    expect(remove.getAttribute("aria-label")).toBe("Remove grasp");
    await click(remove);
    expect(onRemove).toHaveBeenCalledTimes(1);
  });
});

describe("Card, Divider, Kbd, Skeleton, EmptyState", () => {
  test("card header, sunken context, separator role", async () => {
    const { host } = await render(
      <>
        <Card
          title="Export"
          description="Stage 2"
          actions={<button>x</button>}
          variant="sunken"
        >
          body
        </Card>
        <Divider orientation="vertical" />
        <Kbd>⌘K</Kbd>
      </>,
    );
    const card = host.querySelector(".ds-card")!;
    expect(card.querySelector("h3")!.textContent).toBe("Export");
    expect(card.className).toContain("ds-on-sunken");
    const divider = host.querySelector('[role="separator"]')!;
    expect(divider.getAttribute("aria-orientation")).toBe("vertical");
    expect(host.querySelector("kbd")!.textContent).toBe("⌘K");
  });

  test("skeletons are static and hidden from assistive technology", async () => {
    const { host } = await render(
      <>
        <Skeleton width={80} />
        <SkeletonText lines={3} />
      </>,
    );
    const hidden = host.querySelectorAll('[aria-hidden="true"]');
    expect(hidden.length).toBeGreaterThanOrEqual(2);
    expect(host.querySelectorAll(".ds-skeleton")).toHaveLength(4);
    expect(host.innerHTML).not.toContain("shimmer");
  });

  test("empty state gives the next step", async () => {
    const { host } = await render(
      <EmptyState
        icon={Database}
        title="No datasets"
        description="Register one."
        action={<button type="button">Register dataset</button>}
      />,
    );
    expect(host.querySelector(".ds-empty__title")!.textContent).toBe(
      "No datasets",
    );
    expect(host.querySelector("button")!.textContent).toBe("Register dataset");
  });
});

describe("Progress and Spinner", () => {
  test("determinate: progressbar with value, min, max and text", async () => {
    const { host } = await render(
      <Progress label="Converting" value={62} showValue />,
    );
    const bar = host.querySelector('[role="progressbar"]')!;
    expect(bar.getAttribute("aria-label")).toBe("Converting");
    expect(bar.getAttribute("aria-valuenow")).toBe("62");
    expect(bar.getAttribute("aria-valuemin")).toBe("0");
    expect(bar.getAttribute("aria-valuemax")).toBe("100");
    expect(bar.getAttribute("aria-valuetext")).toBe("62%");
    expect(host.textContent).toContain("62%");
    expect(host.querySelector('[role="dialog"]')).toBeNull();
  });

  test("values are clamped", async () => {
    const { host } = await render(<Progress label="x" value={180} />);
    expect(
      host.querySelector('[role="progressbar"]')!.getAttribute("aria-valuenow"),
    ).toBe("100");
  });

  test("indeterminate moves normally, and says 'In progress' when motion is reduced", async () => {
    const restoreNormal = mockMatchMedia([]);
    const normal = await render(<Progress label="Scanning" value={null} />);
    const bar = normal.host.querySelector('[role="progressbar"]')!;
    expect(bar.hasAttribute("aria-valuenow")).toBe(false);
    expect(bar.className).toContain("ds-progress__track--indeterminate");
    expect(bar.className).not.toContain("ds-progress__track--still");
    expect(normal.host.textContent).not.toContain("In progress");
    restoreNormal();

    const restoreReduced = mockMatchMedia(["prefers-reduced-motion"]);
    const reduced = await render(<Progress label="Scanning" value={null} />);
    const still = reduced.host.querySelector('[role="progressbar"]')!;
    expect(still.className).toContain("ds-progress__track--still");
    expect(reduced.host.textContent).toContain("In progress");
    restoreReduced();
  });

  test("spinner is a status with a label, never a dialog", async () => {
    const { host } = await render(<Spinner label="Loading episodes" />);
    const status = host.querySelector('[role="status"]')!;
    expect(status.textContent).toBe("Loading episodes");
    expect(status.querySelector(".ds-sr-only")).not.toBeNull();
    expect(host.querySelector('[role="dialog"]')).toBeNull();
    expect(host.querySelector("[aria-modal]")).toBeNull();
  });
});

describe("Tabs and SegmentedControl", () => {
  function TabsHarness() {
    const [value, setValue] = useState("a");
    return (
      <Tabs
        label="Viewer"
        value={value}
        onChange={setValue}
        items={[
          { id: "a", label: "Episode", content: "A body" },
          { id: "b", label: "Annotate", content: "B body" },
          { id: "c", label: "3D", disabled: true, content: "C body" },
          { id: "d", label: "Analysis", content: "D body" },
        ]}
      />
    );
  }

  test("ARIA tabs: one tab stop, arrows move and select, disabled skipped", async () => {
    const { host } = await render(<TabsHarness />);
    const list = host.querySelector('[role="tablist"]')!;
    expect(list.getAttribute("aria-label")).toBe("Viewer");
    const tabs = () => Array.from(host.querySelectorAll('[role="tab"]'));
    expect(tabs().map((t) => t.getAttribute("tabindex"))).toEqual([
      "0",
      "-1",
      "-1",
      "-1",
    ]);
    const panel = host.querySelector('[role="tabpanel"]')!;
    expect(panel.getAttribute("aria-labelledby")).toBe(tabs()[0].id);
    expect(tabs()[0].getAttribute("aria-controls")).toBe(panel.id);
    await focus(tabs()[0]);
    await press(tabs()[0], "ArrowRight");
    expect(tabs()[1].getAttribute("aria-selected")).toBe("true");
    expect(document.activeElement).toBe(tabs()[1]);
    expect(host.querySelector('[role="tabpanel"]')!.textContent).toBe("B body");
    await press(tabs()[1], "ArrowRight");
    expect(tabs()[3].getAttribute("aria-selected")).toBe("true");
    await press(tabs()[3], "ArrowRight");
    expect(tabs()[0].getAttribute("aria-selected")).toBe("true");
    await press(tabs()[0], "End");
    expect(tabs()[3].getAttribute("aria-selected")).toBe("true");
    await press(tabs()[3], "Home");
    expect(tabs()[0].getAttribute("aria-selected")).toBe("true");
  });

  test("segmented control is a radio group with roving focus", async () => {
    function Harness() {
      const [value, setValue] = useState("timeline");
      return (
        <SegmentedControl
          label="View"
          value={value}
          onChange={setValue}
          options={[
            { value: "timeline", label: "Timeline" },
            { value: "frames", label: "Frames" },
          ]}
        />
      );
    }
    const { host } = await render(<Harness />);
    const group = host.querySelector('[role="radiogroup"]')!;
    expect(group.getAttribute("aria-label")).toBe("View");
    const radios = () => Array.from(host.querySelectorAll('[role="radio"]'));
    expect(radios()[0].getAttribute("aria-checked")).toBe("true");
    await focus(radios()[0]);
    await press(radios()[0], "ArrowRight");
    expect(radios()[1].getAttribute("aria-checked")).toBe("true");
    expect(radios()[1].getAttribute("tabindex")).toBe("0");
    expect(document.activeElement).toBe(radios()[1]);
    await press(radios()[1], "ArrowUp");
    expect(radios()[0].getAttribute("aria-checked")).toBe("true");
  });

  test("theme picker offers system, light and dark", async () => {
    const onChange = mock((value: string) => value);
    const { host } = await render(
      <ThemePicker value="system" onChange={onChange} />,
    );
    const radios = Array.from(host.querySelectorAll('[role="radio"]'));
    expect(radios.map((r) => r.textContent)).toEqual([
      "System",
      "Light",
      "Dark",
    ]);
    await click(radios[2]);
    expect(onChange).toHaveBeenCalledWith("dark");
  });
});

describe("Table", () => {
  test("selected row is marked and announced as current", async () => {
    const { host } = await render(
      <Table caption="Episodes">
        <tbody>
          <TableRow>
            <td>1</td>
          </TableRow>
          <TableRow selected>
            <td>2</td>
          </TableRow>
        </tbody>
      </Table>,
    );
    expect(host.querySelector("caption")!.textContent).toBe("Episodes");
    const rows = host.querySelectorAll("tr");
    expect(rows[0].hasAttribute("data-selected")).toBe(false);
    expect(rows[1].getAttribute("data-selected")).toBe("true");
    expect(rows[1].getAttribute("aria-current")).toBe("true");
    expect(host.querySelector(".ds-table-wrap")).not.toBeNull();
  });
});

describe("review fixes", () => {
  test("only the selected tab names a panel; an unknown value falls back", async () => {
    const { host } = await render(
      <Tabs
        label="Viewer"
        value="missing"
        onChange={() => undefined}
        items={[
          { id: "off", label: "Off", disabled: true, content: "x" },
          { id: "a", label: "A", content: "A body" },
          { id: "b", label: "B", content: "B body" },
        ]}
      />,
    );
    const tabs = Array.from(host.querySelectorAll('[role="tab"]'));
    expect(tabs.map((t) => t.getAttribute("aria-selected"))).toEqual([
      "false",
      "true",
      "false",
    ]);
    expect(tabs.map((t) => t.hasAttribute("aria-controls"))).toEqual([
      false,
      true,
      false,
    ]);
    expect(tabs[1].getAttribute("tabindex")).toBe("0");
    expect(host.querySelector('[role="tabpanel"]')!.textContent).toBe("A body");
    expect(selectedIndex([{ id: "a" }, { id: "b" }], "b")).toBe(1);
    expect(selectedIndex([{ id: "a", disabled: true }, { id: "b" }], "a")).toBe(
      1,
    );
  });

  test("tag remove target is 24 px; a non-text tag must name its button", async () => {
    const onRemove = mock(() => undefined);
    const { host } = await render(
      <Tag onRemove={onRemove} removeLabel="Remove camera wrist">
        <strong>wrist</strong>
      </Tag>,
    );
    const button = host.querySelector("button")!;
    expect(button.getAttribute("aria-label")).toBe("Remove camera wrist");
    expect(button.querySelector(".ds-tag__remove-mark")).not.toBeNull();
    // @ts-expect-error removeLabel is required when the tag is not plain text
    void (
      <Tag onRemove={onRemove}>
        <strong>x</strong>
      </Tag>
    );
  });

  test("Progress follows a ReducedMotionScope", async () => {
    const restore = mockMatchMedia([]);
    try {
      const { host } = await render(
        <ReducedMotionScope reduce>
          <Progress label="Scanning" value={null} />
        </ReducedMotionScope>,
      );
      expect(host.textContent).toContain("In progress");
      expect(host.querySelector(".ds-progress__track--still")).not.toBeNull();
    } finally {
      restore();
    }
  });
});
