"use client";
/**
 * Reorderable list: drag by the handle (Motion `Reorder`), or focus the
 * handle and press ↑/↓ (keyboard and screen-reader users). Every move is
 * announced in a polite live region. Lifting raises the shadow and scales to
 * 1.02; release springs into place (320 ms). Under reduced motion items snap
 * (no scale, no spring).
 *
 * This is the only place the design system uses Motion (user decision
 * 2026-10-04: Motion for drag and list reordering only). Import it lazily
 * (`next/dynamic`) on pages that do not always show it.
 */
import { useId, useState, type KeyboardEvent, type ReactNode } from "react";
import {
  MotionConfig,
  Reorder,
  useDragControls,
  useReducedMotion,
  type Transition,
} from "motion/react";
import { GripVertical } from "lucide-react";
import { useLocale } from "@/components/levi-locale";
import { useReducedMotionOverride } from "@/lib/design/motion";
import { Icon } from "./Icon";
import { cx } from "./internal";

/** Spring close to `--ds-ease-spring` (≤ 6 % overshoot, about 320 ms). */
export const DS_SPRING: Transition = {
  type: "spring",
  stiffness: 520,
  damping: 38,
  mass: 1,
};

/** The transition to use for layout moves given the reduced-motion flag. */
export function dsLayoutTransition(reduced: boolean): Transition {
  return reduced ? { duration: 0 } : DS_SPRING;
}

/** Return a copy of `list` with the item at `from` moved to `to`. */
export function moveItem<T>(list: readonly T[], from: number, to: number): T[] {
  const result = list.slice();
  if (from < 0 || from >= result.length) return result;
  const target = Math.min(Math.max(to, 0), result.length - 1);
  const [item] = result.splice(from, 1);
  result.splice(target, 0, item);
  return result;
}

function Row<T>({
  item,
  index,
  count,
  name,
  helpId,
  reduced,
  onMove,
  children,
}: {
  item: T;
  index: number;
  count: number;
  name: string;
  helpId: string;
  reduced: boolean;
  onMove: (from: number, to: number) => void;
  children: ReactNode;
}) {
  const { t } = useLocale();
  const controls = useDragControls();
  const [dragging, setDragging] = useState(false);
  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    let to: number | null = null;
    if (event.key === "ArrowUp") to = index - 1;
    else if (event.key === "ArrowDown") to = index + 1;
    else if (event.key === "Home") to = 0;
    else if (event.key === "End") to = count - 1;
    if (to === null) return;
    event.preventDefault();
    if (to >= 0 && to < count && to !== index) onMove(index, to);
  };
  return (
    <Reorder.Item
      value={item}
      dragListener={false}
      dragControls={controls}
      layout="position"
      transition={dsLayoutTransition(reduced)}
      whileDrag={reduced ? undefined : { scale: 1.02 }}
      onDragStart={() => setDragging(true)}
      onDragEnd={() => setDragging(false)}
      className="ds-reorder__item"
      data-dragging={dragging || undefined}
    >
      <button
        type="button"
        className="ds-reorder__handle ds-focus"
        aria-label={`${t("Move")} ${name}`}
        aria-describedby={helpId}
        onPointerDown={(event) => controls.start(event)}
        onKeyDown={onKeyDown}
      >
        <Icon icon={GripVertical} />
      </button>
      <div className="ds-reorder__content">{children}</div>
    </Reorder.Item>
  );
}

export function ReorderList<T>({
  items,
  onReorder,
  getKey,
  getLabel,
  renderItem,
  label,
  className,
}: {
  items: T[];
  onReorder: (items: T[]) => void;
  getKey: (item: T) => string;
  /** Plain-text name of an item, for the handle and announcements. */
  getLabel: (item: T) => string;
  renderItem: (item: T, index: number) => ReactNode;
  /** Accessible name of the list. */
  label: string;
  className?: string;
}) {
  const { t, language } = useLocale();
  const forced = useReducedMotionOverride();
  const system = useReducedMotion();
  const reduced = forced ?? Boolean(system);
  const [announcement, setAnnouncement] = useState("");
  const helpId = useId();
  const move = (from: number, to: number) => {
    const next = moveItem(items, from, to);
    onReorder(next);
    const name = getLabel(items[from]);
    setAnnouncement(
      language === "zh"
        ? `已将 ${name} 移到第 ${to + 1} 位，共 ${items.length} 项`
        : `Moved ${name} to position ${to + 1} of ${items.length}`,
    );
  };
  return (
    <MotionConfig
      reducedMotion={
        forced === true ? "always" : forced === false ? "never" : "user"
      }
    >
      <div className={cx("ds-reorder", className)}>
        <p id={helpId} className="ds-sr-only">
          {t("Drag the handle, or focus it and press the up and down arrows.")}
        </p>
        <Reorder.Group
          axis="y"
          values={items}
          onReorder={(next: T[]) => onReorder(next)}
          className="ds-reorder__list"
          aria-label={label}
        >
          {items.map((item, index) => (
            <Row
              key={getKey(item)}
              item={item}
              index={index}
              count={items.length}
              name={getLabel(item)}
              helpId={helpId}
              reduced={reduced}
              onMove={move}
            >
              {renderItem(item, index)}
            </Row>
          ))}
        </Reorder.Group>
        <div role="status" aria-live="polite" className="ds-sr-only">
          {announcement}
        </div>
      </div>
    </MotionConfig>
  );
}
