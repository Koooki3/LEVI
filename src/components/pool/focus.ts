/** Deliberate focus after an action removes or disables the control that
 * had it (a keyboard or screen-reader user would otherwise land on the page
 * top). A row is marked `data-pool-row="<key>"` and its controls
 * `data-pool-focus="<kind>"`. */
function quote(value: string): string {
  return `"${value.replace(/["\\]/g, "\\$&")}"`;
}

/** Focus a control of one row; false when it is not there or is disabled. */
export function focusRowControl(
  root: ParentNode | null,
  row: string,
  kind: string,
): boolean {
  const target = root?.querySelector<HTMLElement>(
    `[data-pool-row=${quote(row)}] [data-pool-focus=${quote(kind)}]:not(:disabled)`,
  );
  if (!target) return false;
  target.focus();
  return true;
}

/** Focus the first of these that exists and can take focus. */
export function focusFirst(
  candidates: Array<HTMLElement | null | undefined>,
): boolean {
  for (const el of candidates) {
    if (el && !(el as HTMLButtonElement).disabled) {
      el.focus();
      return true;
    }
  }
  return false;
}
