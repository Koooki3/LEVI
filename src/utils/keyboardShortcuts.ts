// Modified for LEVI (2026); see NOTICE and docs/UPSTREAM.md.

/** Return whether a keyboard event requests the platform's Save command. */
export function isSaveShortcut(
  event: Pick<KeyboardEvent, "ctrlKey" | "metaKey" | "altKey" | "key">,
): boolean {
  return (
    (event.ctrlKey || event.metaKey) &&
    !event.altKey &&
    event.key.toLowerCase() === "s"
  );
}
