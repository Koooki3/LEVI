/**
 * The frame's own keyboard shortcuts, kept apart from the pages':
 *
 * - ⌘K (macOS) / Ctrl+K (elsewhere) opens the command palette, also from a
 *   text field (it is the standard binding and types nothing);
 * - ? (or the full-width ？, also typed with AltGr) opens the shortcut list,
 *   never while typing.
 *
 * Neither fires during IME composition. Pages bind Space, arrows, J/K,
 * Escape and Ctrl/⌘+S/Z/Y (see SHORTCUT_GROUPS); none of those is used here.
 */
export type GlobalShortcut = "palette" | "shortcuts";

export type KeyLike = {
  key: string;
  ctrlKey?: boolean;
  metaKey?: boolean;
  altKey?: boolean;
  shiftKey?: boolean;
  isComposing?: boolean;
  keyCode?: number;
  target?: EventTarget | null;
};

/** True on Apple platforms, where ⌘ is the command modifier. */
export function isApplePlatform(
  platform: string | undefined = typeof navigator !== "undefined"
    ? navigator.platform || navigator.userAgent
    : "",
): boolean {
  return /Mac|iPhone|iPad|iPod/i.test(platform ?? "");
}

/** Whether keys typed at `target` are text input. */
export function isEditableTarget(target: EventTarget | null | undefined) {
  if (!target || typeof (target as Element).closest !== "function")
    return false;
  const element = target as Element;
  return Boolean(
    element.closest(
      "input, textarea, select, [contenteditable=''], [contenteditable='true'], [role='textbox']",
    ),
  );
}

export function globalShortcut(
  event: KeyLike,
  apple: boolean = isApplePlatform(),
): GlobalShortcut | null {
  // 229: a key that an input method is still processing.
  if (event.isComposing || event.keyCode === 229) return null;
  const key = event.key;
  const command = apple ? event.metaKey : event.ctrlKey;
  const other = apple ? event.ctrlKey : event.metaKey;
  if (
    (key === "k" || key === "K") &&
    command &&
    !other &&
    !event.altKey &&
    !event.shiftKey
  )
    return "palette";
  // "?" or the full-width "？" (Chinese keyboard layouts). Ctrl and Alt
  // together are AltGr, which some layouts need to type "?".
  const altGr = Boolean(event.ctrlKey && event.altKey);
  if (
    (key === "?" || key === "？") &&
    !event.metaKey &&
    (altGr || (!event.ctrlKey && !event.altKey)) &&
    !isEditableTarget(event.target)
  )
    return "shortcuts";
  return null;
}

/** The label of the palette shortcut on this platform. */
export function paletteKeys(apple: boolean = isApplePlatform()): string[] {
  return apple ? ["⌘", "K"] : ["Ctrl", "K"];
}

export type ShortcutRow = { keys: string[][]; label: string };
export type ShortcutGroup = { title: string; rows: ShortcutRow[] };

/**
 * Every shortcut the interface has, for the "?" list. Page shortcuts are
 * listed as the pages define them (episode-viewer.tsx, annotations-panel.tsx,
 * annotations-context.tsx, agent-review-queue.tsx); this list does not bind
 * them. `keys` holds alternatives, each a key combination.
 */
export function shortcutGroups(apple: boolean = isApplePlatform()) {
  const mod = apple ? "⌘" : "Ctrl";
  const groups: ShortcutGroup[] = [
    {
      title: "Everywhere",
      rows: [
        { keys: [paletteKeys(apple)], label: "Open the command palette" },
        { keys: [["?"]], label: "Show keyboard shortcuts" },
        { keys: [["Esc"]], label: "Close the topmost panel or dialog" },
      ],
    },
    {
      title: "Episode viewer",
      rows: [
        { keys: [["Space"]], label: "Play or pause" },
        { keys: [["↑"], ["↓"]], label: "Previous or next episode" },
      ],
    },
    {
      title: "Annotations",
      rows: [
        { keys: [[mod, "S"]], label: "Save the episode" },
        { keys: [[mod, "Z"]], label: "Undo" },
        {
          keys: [
            [mod, "Shift", "Z"],
            [mod, "Y"],
          ],
          label: "Redo",
        },
      ],
    },
    {
      title: "Agent review queue",
      rows: [{ keys: [["J"], ["K"]], label: "Next or previous proposal" }],
    },
  ];
  return groups;
}
