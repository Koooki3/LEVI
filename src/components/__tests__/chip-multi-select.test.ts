import { describe, expect, test } from "bun:test";

/** The range arithmetic the chip selector uses, checked without a DOM.
 *
 * Dragging across chips is how an episode range gets picked, so the order of
 * the anchor and the pointer must not matter, and the result must keep the
 * option order rather than the click order.
 */
function applyRange(
  options: string[],
  selected: string[],
  from: number,
  to: number,
  add: boolean,
): string[] {
  const [start, end] = from <= to ? [from, to] : [to, from];
  const next = new Set(selected);
  for (let index = start; index <= end; index += 1) {
    const value = options[index];
    if (value === undefined) continue;
    if (add) next.add(value);
    else next.delete(value);
  }
  return options.filter((option) => next.has(option));
}

const EPISODES = ["0", "1", "2", "3", "4"];

describe("chip range selection", () => {
  test("dragging forwards selects the run of chips", () => {
    expect(applyRange(EPISODES, [], 1, 3, true)).toEqual(["1", "2", "3"]);
  });

  test("dragging backwards selects the same run", () => {
    expect(applyRange(EPISODES, [], 3, 1, true)).toEqual(["1", "2", "3"]);
  });

  test("dragging from a selected chip removes the run", () => {
    expect(applyRange(EPISODES, ["1", "2", "3"], 2, 3, false)).toEqual(["1"]);
  });

  test("the result keeps dataset order, not click order", () => {
    const picked = applyRange(EPISODES, ["4"], 0, 0, true);
    expect(picked).toEqual(["0", "4"]);
  });

  test("a range beyond the end stops at the last option", () => {
    expect(applyRange(EPISODES, [], 3, 99, true)).toEqual(["3", "4"]);
  });
});
