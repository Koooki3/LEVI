import { press, render, setupDom } from "@/components/ds/__tests__/dom";
import { describe, expect, mock, test } from "bun:test";
import { readFileSync } from "fs";
import { join } from "path";
import { useEscape } from "../use-escape";

setupDom();

function Probe({
  active,
  onEscape,
}: {
  active: boolean;
  onEscape: () => void;
}) {
  useEscape(active, onEscape);
  return <p>probe</p>;
}

describe("enlarged video", () => {
  test("Escape leaves it while active, and only then", async () => {
    const onEscape = mock(() => undefined);
    const { rerender } = await render(<Probe active onEscape={onEscape} />);
    await press(document.body, "Escape");
    expect(onEscape).toHaveBeenCalledTimes(1);
    await press(document.body, "Enter");
    expect(onEscape).toHaveBeenCalledTimes(1);
    await rerender(<Probe active={false} onEscape={onEscape} />);
    await press(document.body, "Escape");
    expect(onEscape).toHaveBeenCalledTimes(1);
  });

  test("a modal layer keeps Escape for itself", async () => {
    const onEscape = mock(() => undefined);
    await render(
      <>
        <Probe active onEscape={onEscape} />
        <div aria-modal="true" />
      </>,
    );
    await press(document.body, "Escape");
    expect(onEscape).not.toHaveBeenCalled();
  });

  test("sits above the top bar (dialog layer), not under it", () => {
    const css = readFileSync(join(import.meta.dir, "../viewer.css"), "utf8");
    const rule = css.slice(css.indexOf(".vw-video-enlarged {"));
    expect(rule.slice(0, rule.indexOf("}"))).toContain(
      "z-index: var(--ds-z-dialog)",
    );
  });
});
