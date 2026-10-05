/**
 * The frame's own keyboard shortcuts, kept apart from the pages':
 *
 * - ⌘K (macOS) / Ctrl+K (elsewhere) opens the command palette, also from a
 *   text field (it is the standard binding and types nothing);
 * - ? (or the full-width ？, also typed with AltGr) opens the shortcut list,
 *   never while typing.
 *
 * - G, then a letter, goes to a page (G H home, G E explore, G W conversion &
 *   review, G P training pool, G L live evaluation, G R report, G U guide):
 *   the second key must follow within CHORD_WINDOW_MS and never fires while
 *   typing.
 *
 * None fires during IME composition. Pages bind Space, arrows, J/K,
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

/** Pages reached by G and a letter. `label` is the catalogue key. */
export const CHORD_PAGES: Record<string, { href: string; label: string }> = {
  h: { href: "/", label: "Home" },
  e: { href: "/explore", label: "Explore" },
  w: { href: "/workbench", label: "Conversion & review" },
  p: { href: "/pool", label: "Training pool" },
  l: { href: "/live", label: "Live evaluation" },
  r: { href: "/report", label: "Report" },
  u: { href: "/guide", label: "Guide" },
};
/** How long after G the letter may come. */
export const CHORD_WINDOW_MS = 1500;

/** A plain letter (no modifier), not typed into a field or an input method. */
function plainLetter(event: KeyLike): string | null {
  if (event.isComposing || event.keyCode === 229) return null;
  if (event.ctrlKey || event.metaKey || event.altKey || event.shiftKey)
    return null;
  if (isEditableTarget(event.target)) return null;
  return event.key.length === 1 ? event.key.toLowerCase() : null;
}

/** Whether this key starts a "go to" chord. */
export function isChordLeader(event: KeyLike): boolean {
  return plainLetter(event) === "g";
}

/** The page the second key of a chord names, or null. */
export function chordPage(event: KeyLike): string | null {
  const letter = plainLetter(event);
  return letter && CHORD_PAGES[letter] ? CHORD_PAGES[letter].href : null;
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
        ...Object.entries(CHORD_PAGES).map(([letter, page]) => ({
          keys: [["G", letter.toUpperCase()]],
          label: `Go to: ${page.label}`,
        })),
        { keys: [["Esc"]], label: "Close the topmost panel or dialog" },
      ],
    },
    {
      title: "Episode viewer",
      rows: [
        { keys: [["Space"]], label: "Play or pause" },
        { keys: [["↑"], ["↓"]], label: "Previous or next episode" },
        { keys: [["←"], ["→"]], label: "Move within an episode list row" },
        {
          keys: [["←"], ["→"]],
          label: "Playhead slider: 0.1 s back or forward",
        },
        {
          keys: [
            ["Shift", "←"],
            ["Shift", "→"],
          ],
          label: "Playhead slider: 1 s back or forward",
        },
        { keys: [["Home"], ["End"]], label: "Playhead slider: start or end" },
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
        {
          keys: [[mod, "Z"]],
          label: "In a text field, Undo is left to the field",
        },
        { keys: [["Esc"]], label: "Clear the selected annotation" },
      ],
    },
    {
      title: "Agent review queue",
      rows: [{ keys: [["J"], ["K"]], label: "Next or previous proposal" }],
    },
  ];
  return groups;
}
