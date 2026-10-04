"use client";
import {
  useCallback,
  useId,
  useRef,
  useState,
  type ReactNode,
  type RefObject,
} from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";
import { useLocale } from "@/components/levi-locale";
import { Button } from "./Button";
import { IconButton } from "./IconButton";
import { cx, keepKeysInside, useModalFocus } from "./internal";

type Layer = {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  description?: ReactNode;
  children?: ReactNode;
  footer?: ReactNode;
  initialFocus?: RefObject<HTMLElement | null>;
  /** Clicking the scrim closes (default true). */
  closeOnScrim?: boolean;
  hideClose?: boolean;
  /**
   * Render into this element (e.g. a themed root) instead of in place.
   * In place keeps the surrounding `data-theme`.
   */
  container?: Element | null;
  className?: string;
};

type ModalProps = {
  /**
   * false: a non-modal layer (no scrim, the page stays usable, focus is not
   * trapped; Escape closes it while focus is inside). Default true.
   */
  modal?: boolean;
  /** Inline width in px (a resizable side sheet). */
  width?: number | null;
};

function ModalLayer({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  initialFocus,
  closeOnScrim = true,
  hideClose = false,
  container,
  className,
  kind,
  role = "dialog",
  modal = true,
  width,
}: Layer &
  ModalProps & {
    kind: string;
    role?: "dialog" | "alertdialog";
  }) {
  const { t } = useLocale();
  const titleId = useId();
  const descriptionId = useId();
  const panel = useRef<HTMLDivElement>(null);
  useModalFocus(panel, open, {
    initialFocus,
    onEscape: onClose,
    trap: modal,
  });
  if (!open) return null;
  const layer = (
    <div
      className={cx(
        "ds-layer",
        `ds-layer--${kind}`,
        !modal && "ds-layer--nonmodal",
      )}
    >
      {modal && (
        <div
          className="ds-scrim"
          aria-hidden="true"
          // Keep focus where it is when the scrim is pressed.
          onMouseDown={(event) => event.preventDefault()}
          onClick={closeOnScrim ? onClose : undefined}
        />
      )}
      <div
        ref={panel}
        role={role}
        aria-modal={modal ? "true" : undefined}
        aria-labelledby={titleId}
        aria-describedby={description ? descriptionId : undefined}
        tabIndex={-1}
        onKeyDown={modal ? keepKeysInside : undefined}
        className={cx(`ds-${kind}`, "ds-on-raised", className)}
        style={width ? { width: `${width}px` } : undefined}
      >
        {/* div, not header/footer: inside a dialog those would be page
            landmarks (banner, contentinfo) to assistive technology. */}
        <div className="ds-dialog__header">
          <h2 id={titleId} className="ds-dialog__title">
            {title}
          </h2>
          {!hideClose && (
            <IconButton
              icon={X}
              label={t("Close")}
              onClick={onClose}
              size="sm"
            />
          )}
        </div>
        {description && (
          <p id={descriptionId} className="ds-dialog__description">
            {description}
          </p>
        )}
        {children && <div className="ds-dialog__body">{children}</div>}
        {footer && <div className="ds-dialog__footer">{footer}</div>}
      </div>
    </div>
  );
  return container ? createPortal(layer, container) : layer;
}

/**
 * A modal dialog: focus moves in and is kept inside (Tab wraps), Escape and
 * the scrim close it, and focus returns to the element that opened it.
 */
export function Dialog({
  size = "md",
  ...props
}: Layer & { size?: "sm" | "md" | "lg" }) {
  return (
    <ModalLayer
      {...props}
      kind="dialog"
      className={cx(`ds-dialog--${size}`, props.className)}
    />
  );
}

/**
 * A side or bottom sheet (drawer) with the same focus rules as Dialog.
 * It slides in from its side; under reduced motion it only appears.
 * `modal={false}` keeps the page beside it usable (no scrim, no focus trap).
 */
export function Sheet({
  side = "right",
  ...props
}: Layer & ModalProps & { side?: "right" | "left" | "bottom" }) {
  return (
    <ModalLayer
      {...props}
      kind="sheet"
      className={cx(`ds-sheet--${side}`, props.className)}
    />
  );
}

export type ConfirmOptions = {
  /** Say the action and its object: "Delete model seg-v3?" */
  title: ReactNode;
  /** Say the consequence. */
  description?: ReactNode;
  /** A verb ("Delete"), never "OK". */
  confirmLabel: string;
  cancelLabel?: string;
  tone?: "default" | "danger";
};

/**
 * A confirmation for an irreversible action. The danger button is never the
 * default focus: focus starts on Cancel for `tone="danger"`. Escape and the
 * scrim cancel. Replaces `window.confirm`; app code asks through
 * `useConfirmAction()` (components/shell/confirm.tsx), one dialog at the root.
 */
export function ConfirmDialog({
  open,
  title,
  description,
  confirmLabel,
  cancelLabel,
  tone = "default",
  busy = false,
  onConfirm,
  onCancel,
  container,
}: ConfirmOptions & {
  open: boolean;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
  container?: Element | null;
}) {
  const { t } = useLocale();
  const cancelRef = useRef<HTMLButtonElement>(null);
  const confirmRef = useRef<HTMLButtonElement>(null);
  return (
    <ModalLayer
      kind="dialog"
      role="alertdialog"
      className="ds-dialog--sm"
      open={open}
      onClose={onCancel}
      title={title}
      description={description}
      hideClose
      container={container}
      initialFocus={tone === "danger" ? cancelRef : confirmRef}
      footer={
        <>
          <Button ref={cancelRef} variant="secondary" onClick={onCancel}>
            {cancelLabel ?? t("Cancel")}
          </Button>
          <Button
            ref={confirmRef}
            variant={tone === "danger" ? "danger" : "primary"}
            loading={busy}
            onClick={onConfirm}
          >
            {confirmLabel}
          </Button>
        </>
      }
    />
  );
}

/**
 * Promise-style confirmation, a drop-in for `window.confirm`:
 *
 *   const { confirm, dialog } = useConfirm();
 *   if (await confirm({ title, confirmLabel: t("Delete"), tone: "danger" })) …
 *   return <>{…}{dialog}</>;
 */
export function useConfirm(container?: Element | null): {
  confirm: (options: ConfirmOptions) => Promise<boolean>;
  dialog: ReactNode;
  /** Close an open question as cancelled (its caller gets false). */
  cancel: () => void;
} {
  const [state, setState] = useState<
    (ConfirmOptions & { resolve: (value: boolean) => void }) | null
  >(null);
  const confirm = useCallback(
    (options: ConfirmOptions) =>
      new Promise<boolean>((resolve) =>
        // A new question replaces one still open: that one counts as
        // cancelled, so its caller is never left waiting.
        setState((previous) => {
          if (previous && previous.resolve !== resolve) previous.resolve(false);
          return { ...options, resolve };
        }),
      ),
    [],
  );
  const settle = (value: boolean) => {
    state?.resolve(value);
    setState(null);
  };
  const cancel = useCallback(
    () =>
      setState((previous) => {
        previous?.resolve(false);
        return null;
      }),
    [],
  );
  const dialog = state ? (
    <ConfirmDialog
      {...state}
      open
      container={container}
      onConfirm={() => settle(true)}
      onCancel={() => settle(false)}
    />
  ) : null;
  return { confirm, dialog, cancel };
}
