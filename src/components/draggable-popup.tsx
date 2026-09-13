// Modified for LEVI (2026); see NOTICE and docs/UPSTREAM.md.
"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import { isSaveShortcut } from "../utils/keyboardShortcuts";

interface DraggablePopupProps {
  children: React.ReactNode;
  header: React.ReactNode;
  onSubmit?: () => void;
  onCancel?: () => void;
  canSubmit?: boolean;
  className?: string;
  ariaLabel?: string;
}

type PopupPosition = { left: number; top: number };

/**
 * Shared shell for annotation input popups.
 *
 * Popups open in the viewport centre so a draw near an edge cannot put the
 * input outside the visible video. The header is a drag handle; once moved,
 * the position is clamped to the viewport and retained for the popup's life.
 * Ctrl/Cmd+S is handled at the shell level so focus on a select or button is
 * covered too, and the browser's Save Page dialog is never allowed to win.
 */
export const DraggablePopup: React.FC<DraggablePopupProps> = ({
  children,
  header,
  onSubmit,
  onCancel,
  canSubmit = true,
  className = "quick-popup",
  ariaLabel = "Annotation input",
}) => {
  const popupRef = useRef<HTMLDivElement | null>(null);
  const dragRef = useRef<{
    pointerId: number;
    offsetX: number;
    offsetY: number;
  } | null>(null);
  const [position, setPosition] = useState<PopupPosition | null>(null);
  const [dragging, setDragging] = useState(false);

  const clampPosition = useCallback((left: number, top: number) => {
    const popup = popupRef.current;
    const width = popup?.offsetWidth ?? 0;
    const height = popup?.offsetHeight ?? 0;
    const margin = 8;
    return {
      left: Math.min(
        Math.max(margin, left),
        Math.max(margin, window.innerWidth - width - margin),
      ),
      top: Math.min(
        Math.max(margin, top),
        Math.max(margin, window.innerHeight - height - margin),
      ),
    };
  }, []);

  const finishDrag = useCallback(() => {
    dragRef.current = null;
    setDragging(false);
  }, []);

  useEffect(() => {
    if (!dragging) return;

    const move = (event: PointerEvent) => {
      const drag = dragRef.current;
      if (!drag || event.pointerId !== drag.pointerId) return;
      event.preventDefault();
      setPosition(
        clampPosition(
          event.clientX - drag.offsetX,
          event.clientY - drag.offsetY,
        ),
      );
    };
    const end = (event: PointerEvent) => {
      if (dragRef.current?.pointerId !== event.pointerId) return;
      finishDrag();
    };

    window.addEventListener("pointermove", move, { passive: false });
    window.addEventListener("pointerup", end);
    window.addEventListener("pointercancel", end);
    return () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", end);
      window.removeEventListener("pointercancel", end);
    };
  }, [clampPosition, dragging, finishDrag]);

  useEffect(() => {
    const onResize = () => {
      setPosition((current) =>
        current ? clampPosition(current.left, current.top) : current,
      );
    };
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [clampPosition]);

  const beginDrag = (event: React.PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    const target = event.target as HTMLElement;
    if (target.closest("input, button, select, textarea, a")) return;
    const popup = popupRef.current;
    if (!popup) return;
    const rect = popup.getBoundingClientRect();
    setPosition(clampPosition(rect.left, rect.top));
    dragRef.current = {
      pointerId: event.pointerId,
      offsetX: event.clientX - rect.left,
      offsetY: event.clientY - rect.top,
    };
    setDragging(true);
    event.preventDefault();
    event.stopPropagation();
    event.currentTarget.setPointerCapture?.(event.pointerId);
  };

  const onKeyDownCapture = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (isSaveShortcut(event)) {
      event.preventDefault();
      event.stopPropagation();
      if (canSubmit) onSubmit?.();
      return;
    }
    if (event.key === "Escape" && onCancel) {
      event.preventDefault();
      event.stopPropagation();
      onCancel();
    }
  };

  return (
    <div
      ref={popupRef}
      className={`${className}${dragging ? " is-dragging" : ""}`}
      style={
        position
          ? {
              left: position.left,
              top: position.top,
              position: "fixed",
              transform: "none",
            }
          : undefined
      }
      role="dialog"
      aria-label={ariaLabel}
      aria-keyshortcuts="Control+S Meta+S Escape"
      onPointerDown={(event) => event.stopPropagation()}
      onKeyDownCapture={onKeyDownCapture}
    >
      <div
        className="quick-popup-head quick-popup-drag-handle"
        onPointerDown={beginDrag}
        title="Drag to move"
      >
        {header}
        <span className="quick-popup-grip" aria-hidden="true">
          ⋮⋮
        </span>
      </div>
      {children}
    </div>
  );
};
