"use client";
// The confirm window of every start and stop of the marvel0 page: a title, the
// hazard sentences of the action, the checks only a person can make (tick
// boxes; they are not typing) and two buttons. Cancel has the initial focus,
// so a stray Enter cancels; Confirm is on once every tick is set. Escape and
// the backdrop cancel. A second click while the first one is on its way sends
// nothing (the callers' own busy guards are a second layer).
//
// Nothing here asks for a phrase. The confirmation phrase of an action is a
// constant of the backend's catalogue that the page sends itself; the backend
// still checks it. When it refuses it (`confirm_required`) that is an internal
// error of the page, worded as one (`ConfirmRefusal`), never a prompt to type.
// The two exceptions that cut something in use (the override phrases of
// stopping C and of stopping the policy server with a client connected) are
// typed fields their callers put into `after`.
import { useRef, useState, type ReactNode, type RefObject } from "react";
import type { LucideIcon } from "lucide-react";
import { Button, Checkbox, Dialog } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { Note } from "@/components/pages-ui/feedback";
import type { ApiFailure } from "./deploy-api";

export interface ConfirmAttest {
  /** The key sent in `attest`. */
  code: string;
  label: string;
}

export interface ConfirmWindowProps {
  /** What the window is for; null keeps it closed. Ticks live for one opening. */
  open: boolean;
  testId?: string;
  title: string;
  intro?: string;
  /** The fixed sentence about the physical e-stop (a warning note). */
  estop?: string | null;
  /** The facts of the action (a `dl`), above the hazards. */
  facts?: ReactNode;
  /** The sentences about what the action does to the machine, most important
   * first. */
  hazards?: ReactNode[];
  /** Muted lines after the hazards (what the gates are worth, what has not
   * been verified). */
  notes?: ReactNode[];
  attest?: ConfirmAttest[];
  /** More controls of the action below the ticks (an override field, a tick of
   * the caller's own). */
  after?: ReactNode;
  /** The caller's own condition on the Confirm button, besides the ticks. */
  ready?: boolean;
  confirmLabel: string;
  confirmIcon?: LucideIcon;
  /** The action cuts something off: the Confirm button is the danger one. */
  danger?: boolean;
  /** The write is on its way: the buttons wait. */
  working?: boolean;
  /** The refusal of the write this window sent, shown inside it. */
  refused?: ReactNode;
  onCancel: () => void;
  /** Sends the action; the ticks set are passed. May return the promise of the
   * write, which the window waits for before it takes another click. */
  onConfirm: (ticked: Record<string, boolean>) => unknown;
}

function Body({
  testId = "marvel0-confirm",
  title,
  intro,
  estop,
  facts,
  hazards = [],
  notes = [],
  attest = [],
  after,
  ready = true,
  confirmLabel,
  confirmIcon,
  danger = false,
  working = false,
  refused,
  onCancel,
  onConfirm,
}: ConfirmWindowProps) {
  const { t } = useLocale();
  const [ticked, setTicked] = useState<Record<string, boolean>>({});
  const cancel = useRef<HTMLButtonElement>(null);
  const sending = useRef(false);
  const allTicked = attest.every((item) => ticked[item.code] === true);
  const left = attest.filter((item) => ticked[item.code] !== true).length;
  const on = allTicked && ready && !working;
  const send = () => {
    if (!on || sending.current) return;
    sending.current = true;
    let sent: unknown;
    try {
      sent = onConfirm(ticked);
    } catch (error) {
      sending.current = false;
      throw error;
    }
    void Promise.resolve(sent).finally(() => {
      sending.current = false;
    });
  };
  return (
    <Dialog
      open
      onClose={() => {
        if (!working) onCancel();
      }}
      initialFocus={cancel as RefObject<HTMLElement | null>}
      title={title}
      description={intro}
      className="rg-confirm"
      footer={
        <>
          {left > 0 && (
            // In a window taller than the screen the boxes may be out of
            // sight: the footer says why Confirm is off.
            <span
              className="rg-muted rg-confirm__need"
              role="status"
              data-testid={`${testId}-ticks-left`}
            >
              {t("marvel0.ctl.confirm.ticks_left")
                .replace("{n}", String(left))
                .replace("{total}", String(attest.length))}
            </span>
          )}
          <Button
            ref={cancel}
            variant="secondary"
            disabled={working}
            onClick={onCancel}
          >
            {t("marvel0.ctl.btn.cancel")}
          </Button>
          <Button
            variant={danger ? "danger" : "primary"}
            icon={confirmIcon}
            loading={working}
            disabled={!on}
            onClick={send}
          >
            {confirmLabel}
          </Button>
        </>
      }
    >
      {/* The text between the title and the buttons scrolls when the window
          is taller than the screen; a keyboard reaches it by this tab stop. */}
      <div
        data-testid={testId}
        className="rg-dialog"
        tabIndex={0}
        role="group"
        aria-label={title}
      >
        {estop && (
          <Note tone="warning" role="alert">
            {estop}
          </Note>
        )}
        {facts}
        {hazards.length > 0 && (
          <ul className="rg-notes" data-testid={`${testId}-hazards`}>
            {hazards.map((hazard, index) => (
              <li key={index}>{hazard}</li>
            ))}
          </ul>
        )}
        {notes.map((note, index) => (
          <p key={index} className="rg-muted">
            {note}
          </p>
        ))}
        {attest.map((item) => (
          <Checkbox
            key={item.code}
            checked={ticked[item.code] === true}
            onChange={(event) =>
              setTicked((now) => ({
                ...now,
                [item.code]: event.target.checked,
              }))
            }
            label={item.label}
          />
        ))}
        {after}
      </div>
      {/* The refusal of the write stays below the scrolling text, in view: a
          window taller than the screen would hide it at its foot. */}
      {refused}
    </Dialog>
  );
}

export function ConfirmWindow(props: ConfirmWindowProps) {
  // Mounted anew for every opening, so no tick outlives its window.
  return props.open ? <Body {...props} /> : null;
}

/** The refusal of a write, inside its window. A `confirm_required` is the
 * page's own mistake (the phrase it sent is not the one the server holds), so
 * it is said as an internal error with the phrase the server expected, not as
 * something to type. */
export function ConfirmRefusal({
  failure,
  testId = "marvel0-confirm-refused",
  children,
}: {
  failure: ApiFailure;
  testId?: string;
  /** The refusal in the page's words, for every other code. */
  children: ReactNode;
}) {
  const { t } = useLocale();
  const internal = failure.code === "confirm_required";
  const expected =
    typeof failure.extra?.phrase === "string" ? failure.extra.phrase : null;
  return (
    <p
      className="rg-bad"
      role="alert"
      data-testid={testId}
      data-code={failure.code}
      data-internal={internal || undefined}
    >
      {internal ? (
        <>
          {t("marvel0.ctl.confirm.internal")}
          {expected
            ? ` ${t("marvel0.ctl.confirm.internal_expected").replace("{phrase}", expected)}`
            : ""}
        </>
      ) : (
        children
      )}
    </p>
  );
}
