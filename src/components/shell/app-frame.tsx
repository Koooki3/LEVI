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
import { usePathname } from "next/navigation";
import { ToastProvider } from "@/components/ds";
import { ConfirmProvider } from "./confirm";
import { recordVisit } from "./recent";
import { ShellProvider } from "./shell-context";

function VisitRecorder() {
  const pathname = usePathname();
  useEffect(() => {
    if (pathname) recordVisit(pathname);
  }, [pathname]);
  return null;
}

export function AppFrame({ children }: { children: ReactNode }) {
  return (
    <ShellProvider>
      <ToastProvider>
        <ConfirmProvider>
          <VisitRecorder />
          {children}
        </ConfirmProvider>
      </ToastProvider>
    </ShellProvider>
  );
}
