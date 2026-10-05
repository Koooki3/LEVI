"use client";
import { useId } from "react";
import { Button } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";

/**
 * The Cancel / Add buttons of a quick-create popup (a drag on the timeline,
 * a box on the video). Add is off until there is a label, and the reason is
 * written next to it (`aria-describedby`), never only in a tooltip.
 */
export function PopupActions({
  canAdd,
  onCancel,
  onAdd,
}: {
  canAdd: boolean;
  onCancel: () => void;
  onAdd: () => void;
}) {
  const { t } = useLocale();
  const reasonId = useId();
  return (
    <div className="quick-popup-actions">
      {!canAdd && (
        <small id={reasonId} className="vw-a-hint">
          {t("Type a label to add it.")}
        </small>
      )}
      <Button size="sm" variant="ghost" onClick={onCancel}>
        {t("Cancel")}
      </Button>
      <Button
        size="sm"
        variant="primary"
        onClick={onAdd}
        disabled={!canAdd}
        aria-describedby={canAdd ? undefined : reasonId}
        aria-keyshortcuts="Enter"
      >
        {t("Add")}
      </Button>
    </div>
  );
}
