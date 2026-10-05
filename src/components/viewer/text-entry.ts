/**
 * Whether keyboard focus (or an event target) is a place where the person is
 * typing: an input, textarea, select or a contenteditable element. Global
 * shortcuts (undo, Escape to deselect, Space) must leave these alone so the
 * browser's own undo and the field's own Escape keep working.
 */
export function isTextEntry(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  if (!el || typeof el.tagName !== "string") return false;
  const tag = el.tagName;
  if (tag === "TEXTAREA" || tag === "SELECT") return true;
  if (tag === "INPUT") {
    const type = (el as HTMLInputElement).type;
    return ![
      "button",
      "checkbox",
      "radio",
      "range",
      "submit",
      "reset",
      "file",
      "image",
    ].includes(type);
  }
  return (
    el.isContentEditable === true ||
    el.getAttribute?.("contenteditable") === "" ||
    el.getAttribute?.("contenteditable") === "true"
  );
}
