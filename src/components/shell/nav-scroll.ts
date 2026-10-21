/**
 * Where the header's link row (a scrolling row below 1000 px) should be rolled
 * to so that the current page's link is whole. Pure, so it is tested without a
 * DOM.
 *
 * The row is left where it is when the link is already whole in the visible
 * part and no link is cut at the bar's start (rolling it for no reason cut the
 * first link: "ion 1" for "Live evaluation 1" at 420 px). When it has to
 * roll, it rolls to the start of a link, so that the link at the start of the
 * bar is never cut either, and no further than the current link's own start.
 */
export function navScrollTarget(options: {
  /** Start and end of each link, in px from the start of the row's content. */
  edges: ReadonlyArray<readonly [number, number]>;
  /** Index of the current page's link in `edges`. */
  current: number;
  /** How far the row is rolled now. */
  scrollLeft: number;
  /** The visible width of the row. */
  width: number;
}): number | null {
  const link = options.edges[options.current];
  if (!link) return null;
  const [start, end] = link;
  // A link that the bar's start edge runs through (the links changed width
  // after the row was rolled: the live count arrived) is cut as well.
  const cut = options.edges.some(
    ([from, to]) =>
      from < options.scrollLeft - 0.5 && to > options.scrollLeft + 0.5,
  );
  if (
    !cut &&
    start >= options.scrollLeft &&
    end <= options.scrollLeft + options.width
  )
    return null;
  // The least roll that shows the link's end ...
  const least = end - options.width;
  // ... taken up to the next link's start, as long as the link's own start is
  // still in view; a link wider than the row is put at the row's start.
  const aligned = options.edges
    .map(([from]) => from)
    .find((from) => from >= least && from <= start);
  return Math.max(0, aligned ?? start);
}
