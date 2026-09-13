// Modified for LEVI (2026); see NOTICE and docs/UPSTREAM.md.

type SaveShortcutEvent = Pick<
  KeyboardEvent,
  "ctrlKey" | "metaKey" | "altKey" | "key"
> & {
  /** IME composition may emit a synthetic key value that must not submit. */
  isComposing?: boolean;
};

/** Return whether a keyboard event requests the platform's Save command. */
export function isSaveShortcut(event: SaveShortcutEvent): boolean {
  return (
    (event.ctrlKey || event.metaKey) &&
    !event.altKey &&
    event.isComposing !== true &&
    event.key.toLowerCase() === "s"
  );
}
