"use client";
/**
 * The providers of the global frame, mounted once by the root layout:
 * theme and frame state (ShellProvider), the toast region (ToastProvider:
 * `useToast()` from "@/components/ds" anywhere below shows one), and the one
 * confirmation dialog (`useConfirmAction()` from "./confirm").
 */
import type { ReactNode } from "react";
import { ToastProvider } from "@/components/ds";
import { ConfirmProvider } from "./confirm";
import { ShellProvider } from "./shell-context";

export function AppFrame({ children }: { children: ReactNode }) {
  return (
    <ShellProvider>
      <ToastProvider>
        <ConfirmProvider>{children}</ConfirmProvider>
      </ToastProvider>
    </ShellProvider>
  );
}
