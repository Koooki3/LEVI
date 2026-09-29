"use client";
import { useEffect, useId, useRef, type ReactNode } from "react";
import { useLocale } from "@/components/levi-locale";

/** A modal confirmation. Focus starts on Cancel (a destructive button is
 * never the default), Esc cancels, and the browser keeps focus inside. */
export function ConfirmDialog({
  open,
  title,
  children,
  confirmLabel,
  danger = false,
  busy = false,
  disabled = false,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: string;
  children: ReactNode;
  confirmLabel: string;
  danger?: boolean;
  busy?: boolean;
  disabled?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const { t } = useLocale();
  const titleId = useId();
  const ref = useRef<HTMLDialogElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) {
      dialog.showModal();
      cancelRef.current?.focus();
    } else if (!open && dialog.open) dialog.close();
  }, [open]);
  return (
    <dialog
      ref={ref}
      className="levi-pool-dialog"
      aria-labelledby={titleId}
      onCancel={(e) => {
        e.preventDefault();
        onCancel();
      }}
      onClose={() => {
        if (open) onCancel();
      }}
    >
      <div className="levi-pool-dialog-head">
        <h2 id={titleId}>{title}</h2>
      </div>
      <div className="levi-pool-confirm-body">{children}</div>
      <div className="levi-row levi-pool-confirm-actions">
        <button
          ref={cancelRef}
          type="button"
          className="levi-secondary"
          onClick={onCancel}
        >
          {t("Cancel")}
        </button>
        <button
          type="button"
          className={danger ? "levi-pool-danger" : "levi-primary"}
          disabled={busy || disabled}
          onClick={onConfirm}
        >
          {busy ? t("Working…") : confirmLabel}
        </button>
      </div>
    </dialog>
  );
}
