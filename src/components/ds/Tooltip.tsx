"use client";
import {
  cloneElement,
  isValidElement,
  useEffect,
  useId,
  useRef,
  useState,
  type ReactElement,
  type ReactNode,
} from "react";
import { cx } from "./internal";

const OPEN_DELAY_MS = 500;
/** A focus that follows a navigation key within this long is the keyboard's. */
const KEYBOARD_FOCUS_MS = 600;
const NAVIGATION_KEYS = new Set([
  "Tab",
  "ArrowUp",
  "ArrowDown",
  "ArrowLeft",
  "ArrowRight",
  "Home",
  "End",
  "PageUp",
  "PageDown",
]);

// Focus moved by the program (a dialog or drawer putting focus on its first
// control when it opens) must not pop a tooltip: it is not the person
// looking at that control, and the layer's edge would cut it off. Only a
// focus that comes right after a navigation key counts as keyboard focus;
// a pointer press or any other key (Enter that opened the layer) does not.
let lastNavigationKeyAt = -Infinity;
let trackedDocument: Document | null = null;
function trackInputModality() {
  if (typeof document === "undefined" || trackedDocument === document) return;
  // One listener pair per document (tests swap the document between files).
  trackedDocument = document;
  document.addEventListener(
    "keydown",
    (event) => {
      lastNavigationKeyAt = NAVIGATION_KEYS.has(event.key)
        ? performance.now()
        : -Infinity;
    },
    true,
  );
  document.addEventListener(
    "pointerdown",
    () => {
      lastNavigationKeyAt = -Infinity;
    },
    true,
  );
}
function focusIsFromKeyboard(): boolean {
  return performance.now() - lastNavigationKeyAt < KEYBOARD_FOCUS_MS;
}

type TriggerProps = {
  "aria-describedby"?: string;
};

/**
 * A tooltip that keyboard users see too: it opens on hover (after 0.5 s) and
 * at once on keyboard focus (a Tab or arrow key moved it; not a focus the
 * program set, e.g. a dialog's first control), closes on pointer leave, focus loss and Escape. Use it instead
 * of `title=`. The trigger gets `aria-describedby` unless `describe` is false
 * (for an icon button whose tooltip repeats its `aria-label`).
 */
export function Tooltip({
  content,
  children,
  placement = "top",
  describe = true,
  shortcut,
}: {
  content: ReactNode;
  children: ReactElement<TriggerProps>;
  placement?: "top" | "bottom";
  describe?: boolean;
  shortcut?: ReactNode;
}) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pointerDown = useRef(false);

  const clear = () => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = null;
  };
  useEffect(() => clear, []);
  useEffect(() => trackInputModality(), []);
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open]);

  const trigger = isValidElement(children)
    ? cloneElement(children, {
        "aria-describedby": describe
          ? cx(children.props["aria-describedby"], id) || undefined
          : children.props["aria-describedby"],
      })
    : children;

  return (
    <span
      className="ds-tooltip-anchor"
      onPointerEnter={() => {
        clear();
        timer.current = setTimeout(() => setOpen(true), OPEN_DELAY_MS);
      }}
      onPointerLeave={() => {
        clear();
        setOpen(false);
      }}
      onPointerDown={() => {
        pointerDown.current = true;
      }}
      onFocus={() => {
        clear();
        // Only keyboard focus opens it: not a click, not a focus the program
        // moved (see focusIsFromKeyboard).
        if (!pointerDown.current && focusIsFromKeyboard()) setOpen(true);
        pointerDown.current = false;
      }}
      onBlur={() => {
        clear();
        setOpen(false);
      }}
    >
      {trigger}
      <span
        id={id}
        role="tooltip"
        className={cx("ds-tooltip ds-on-raised", `ds-tooltip--${placement}`)}
        data-open={open || undefined}
        hidden={!open}
        aria-hidden={describe ? undefined : true}
      >
        {content}
        {shortcut && <span className="ds-tooltip__shortcut">{shortcut}</span>}
      </span>
    </span>
  );
}
