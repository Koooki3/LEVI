import { describe, expect, test } from "bun:test";
import { navScrollTarget } from "../nav-scroll";

// Links of a 420 px bar, as (start, end) from the start of the row: "Live
// evaluation 1", "Automatic evaluation", "MARVEL0", "Explore", "Conversion &
// review".
const edges = [
  [0, 150],
  [150, 300],
  [300, 380],
  [380, 450],
  [450, 600],
] as const;

describe("rolling the link row to the current page", () => {
  test("a current link that is whole in view leaves the bar where it is (rolling it cut the first link: 'ion 1')", () => {
    expect(
      navScrollTarget({ edges, current: 2, scrollLeft: 0, width: 400 }),
    ).toBeNull();
    // Whole in view when the bar is already rolled, too.
    expect(
      navScrollTarget({ edges, current: 3, scrollLeft: 150, width: 400 }),
    ).toBeNull();
  });

  test("a link that is cut at the end rolls the row to the start of a link, so the first visible link is whole", () => {
    // MARVEL0 ends at 380: 360 px of bar does not show it.
    const target = navScrollTarget({
      edges,
      current: 2,
      scrollLeft: 0,
      width: 360,
    });
    // The least roll is 20 px; the next link start is 150, and the current
    // link (300) is still in view from there.
    expect(target).toBe(150);
  });

  test("a link cut at the start rolls back to a link start", () => {
    expect(
      navScrollTarget({ edges, current: 1, scrollLeft: 200, width: 400 }),
    ).toBe(0);
  });

  test("a link cut at the bar's start is not left like that even when the current link is whole (the links grew after the row was rolled)", () => {
    // Rolled to 118 when "Live evaluation" was 118 wide; it then grew to 150.
    expect(
      navScrollTarget({ edges, current: 3, scrollLeft: 118, width: 400 }),
    ).toBe(150);
  });

  test("a link wider than the bar is put at the start of the bar; no current link: nothing", () => {
    expect(
      navScrollTarget({ edges, current: 4, scrollLeft: 0, width: 100 }),
    ).toBe(450);
    expect(
      navScrollTarget({ edges, current: -1, scrollLeft: 0, width: 100 }),
    ).toBeNull();
  });
});
