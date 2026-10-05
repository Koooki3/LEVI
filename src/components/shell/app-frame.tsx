"use client";
/**
 * The providers of the global frame, mounted once by the root layout:
 * theme and frame state (ShellProvider), the toast region (ToastProvider:
 * `useToast()` from "@/components/ds" anywhere below shows one), and the one
 * confirmation dialog (`useConfirmAction()` from "./confirm"). It also
 * remembers the datasets and episodes opened in this browser for the home
 * page (./recent.ts).
 */
import { useEffect, type ReactNode } from "react";
import { usePathname, useRouter } from "next/navigation";
import { ToastProvider } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { ConfirmProvider, useConfirmAction } from "./confirm";
import { recordVisit } from "./recent";
import { ShellProvider } from "./shell-context";
import { SHELL_EVENTS, requestGo } from "./shell-events";
import { hasUnsavedWork } from "./unsaved-work";

function VisitRecorder() {
  const pathname = usePathname();
  useEffect(() => {
    if (pathname) recordVisit(pathname);
  }, [pathname]);
  return null;
}

/**
 * Goes where a keyboard jump (G then a letter) asks, but first asks when the
 * page holds work that is not saved: a jump is a key press away from losing
 * an annotation draft.
 */
function KeyboardJumps() {
  const router = useRouter();
  const confirm = useConfirmAction();
  const { t } = useLocale();
  useEffect(() => {
    const onGo = async (event: Event) => {
      const href = (event as CustomEvent<string>).detail;
      if (typeof href !== "string") return;
      if (
        hasUnsavedWork() &&
        !(await confirm({
          title: t("Leave this page without saving?"),
          description: t(
            "Your annotation edits are not saved to the workspace yet; they stay only in this browser tab.",
          ),
          confirmLabel: t("Leave without saving"),
          cancelLabel: t("Stay here"),
          tone: "danger",
        }))
      )
        return;
      router.push(href);
    };
    window.addEventListener(SHELL_EVENTS.go, onGo);
    return () => window.removeEventListener(SHELL_EVENTS.go, onGo);
  }, [confirm, router, t]);
  return null;
}

export function AppFrame({ children }: { children: ReactNode }) {
  return (
    <ShellProvider navigate={requestGo}>
      <ToastProvider>
        <ConfirmProvider>
          <VisitRecorder />
          <KeyboardJumps />
          {children}
        </ConfirmProvider>
      </ToastProvider>
    </ShellProvider>
  );
}
