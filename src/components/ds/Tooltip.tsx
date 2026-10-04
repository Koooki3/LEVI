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

type TriggerProps = {
  "aria-describedby"?: string;
};

/**
 * A tooltip that keyboard users see too: it opens on hover (after 0.5 s) and
 * at once on keyboard focus, closes on pointer leave, focus loss and Escape. Use it instead
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
        // Only keyboard focus opens it: a click should not pop the tooltip.
        if (!pointerDown.current) setOpen(true);
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
