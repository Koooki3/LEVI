/**
 * Whether something the person typed or drew is not saved yet (the annotation
 * editor's draft). The pages that hold such work say so with
 * `setUnsavedWork(true)`; the frame asks before a keyboard jump (G then a
 * letter) would leave it behind. A module-level flag: the frame sits above the
 * pages and cannot read their state.
 */
let unsaved = false;
export function setUnsavedWork(value: boolean): void {
  unsaved = value;
}
export function hasUnsavedWork(): boolean {
  return unsaved;
}
