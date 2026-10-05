import { click, press, render, setupDom } from "@/components/ds/__tests__/dom";
import { describe, expect, test } from "bun:test";
import { AnnotationsProvider } from "@/context/annotations-context";
import { TimeProvider, useTime } from "@/context/time-context";
import { LocaleProvider } from "@/components/levi-locale";
import { AnnotationsTimeline } from "@/components/annotations-timeline";

setupDom();

function Clock() {
  const { currentTime } = useTime();
  return <output data-testid="clock">{currentTime.toFixed(2)}</output>;
}

function page() {
  return (
    <LocaleProvider>
      <TimeProvider duration={10}>
        <AnnotationsProvider>
          <Clock />
          <AnnotationsTimeline duration={10} />
        </AnnotationsProvider>
      </TimeProvider>
    </LocaleProvider>
  );
}

describe("timeline playhead handle", () => {
  test("is a labelled slider with a tooltip, not a title-only div", async () => {
    const { host } = await render(page());
    const handle = host.querySelector<HTMLElement>(".tl-playhead-handle")!;
    expect(handle.getAttribute("role")).toBe("slider");
    expect(handle.getAttribute("aria-label")).toBe("Playhead");
    expect(handle.getAttribute("aria-valuemin")).toBe("0");
    expect(handle.getAttribute("aria-valuemax")).toBe("10");
    expect(handle.getAttribute("aria-valuenow")).toBe("0");
    expect(handle.tabIndex).toBe(0);
    expect(handle.hasAttribute("title")).toBe(false);
    const tip = handle
      .closest(".ds-tooltip-anchor")!
      .querySelector("[role=tooltip]")!;
    expect(tip.textContent).toBe("Drag to scrub, or use the arrow keys");
  });

  test("arrow keys nudge the playhead, Shift moves a second, ends are clamped", async () => {
    const { host } = await render(page());
    const handle = host.querySelector<HTMLElement>(".tl-playhead-handle")!;
    const clock = () => host.querySelector("[data-testid=clock]")!.textContent;
    await press(handle, "ArrowRight");
    expect(clock()).toBe("0.10");
    await press(handle, "ArrowRight", { shiftKey: true });
    expect(clock()).toBe("1.10");
    await press(handle, "ArrowLeft");
    expect(clock()).toBe("1.00");
    await press(handle, "End");
    expect(clock()).toBe("10.00");
    await press(handle, "ArrowRight", { shiftKey: true });
    expect(clock()).toBe("10.00");
    await press(handle, "Home");
    await press(handle, "ArrowLeft");
    expect(clock()).toBe("0.00");
    expect(handle.getAttribute("aria-valuenow")).toBe("0");
  });

  test("other keys are left alone", async () => {
    const { host } = await render(page());
    const handle = host.querySelector<HTMLElement>(".tl-playhead-handle")!;
    await click(handle);
    await press(handle, "a");
    expect(host.querySelector("[data-testid=clock]")!.textContent).toBe("0.00");
  });
});
