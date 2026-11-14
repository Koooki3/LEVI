import { describe, expect, test } from "bun:test";
import { render, setupDom } from "@/components/ds/__tests__/dom";
import { useOpenWhen } from "../use-open-when";

setupDom();

function Fold({ red, withFold }: { red: boolean; withFold: boolean }) {
  const ref = useOpenWhen<HTMLDetailsElement>(red);
  return withFold ? (
    <details ref={ref}>
      <summary>Details</summary>
      reasons
    </details>
  ) : (
    <p>no reasons yet</p>
  );
}

describe("a fold that opens by itself when something goes wrong", () => {
  test("a fold that appears after the card is already red opens when it appears", async () => {
    const { host, rerender } = await render(<Fold red withFold={false} />);
    await rerender(<Fold red withFold />);
    expect(host.querySelector("details")!.open).toBe(true);
  });

  test("it opens when the state turns red and is never closed for the reader", async () => {
    const { host, rerender } = await render(<Fold red={false} withFold />);
    const fold = host.querySelector("details")!;
    expect(fold.open).toBe(false);
    await rerender(<Fold red withFold />);
    expect(fold.open).toBe(true);
    await rerender(<Fold red={false} withFold />);
    expect(fold.open).toBe(true);
  });
});
