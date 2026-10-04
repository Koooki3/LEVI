"use client";
/**
 * One confirmation dialog for the whole interface, mounted by the root
 * layout. It replaces `window.confirm`:
 *
 *   const confirm = useConfirmAction();
 *   if (!(await confirm({ title, confirmLabel: t("Delete"), tone: "danger" })))
 *     return;
 *
 * Cancel, Escape and the scrim all answer false, as `window.confirm` did
 * for Cancel, and so does a page change while it is open. While it is open,
 * keys typed in it do not reach the page's shortcuts. Outside a ConfirmProvider every question answers false, so an
 * action that needs a yes is never taken without one.
 *
 * The dialog lives at the root, outside any page's styles (an older panel's
 * `button` rules would otherwise restyle its buttons). While a native modal
 * `<dialog>` is open (the training pool's push dialog), everything outside it
 * is inert, so the question is rendered inside that dialog instead.
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { usePathname } from "next/navigation";
import { useConfirm, type ConfirmOptions } from "@/components/ds";

export type ConfirmAction = (options: ConfirmOptions) => Promise<boolean>;

const ConfirmContext = createContext<ConfirmAction | null>(null);

const declined: ConfirmAction = () => Promise.resolve(false);

/** The innermost open modal `<dialog>`, if any (it makes the rest inert). */
export function openModalDialog(): HTMLDialogElement | null {
  const open = Array.from(
    document.querySelectorAll<HTMLDialogElement>("dialog[open]"),
  ).filter((dialog) => {
    try {
      return dialog.matches(":modal");
    } catch {
      return false; // a browser without :modal: keep the root placement
    }
  });
  return open[open.length - 1] ?? null;
}

export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [container, setContainer] = useState<Element | null>(null);
  const { confirm: ask, dialog, cancel } = useConfirm(container);
  // Leaving the page leaves its question unanswered: answer it "no".
  const pathname = usePathname();
  const shownAt = useRef(pathname);
  useEffect(() => {
    if (shownAt.current === pathname) return;
    shownAt.current = pathname;
    cancel();
  }, [pathname, cancel]);
  const confirm = useCallback<ConfirmAction>(
    (options) => {
      setContainer(openModalDialog());
      return ask(options);
    },
    [ask],
  );
  return (
    <ConfirmContext.Provider value={confirm}>
      {children}
      {dialog}
    </ConfirmContext.Provider>
  );
}

export function useConfirmAction(): ConfirmAction {
  return useContext(ConfirmContext) ?? declined;
}
