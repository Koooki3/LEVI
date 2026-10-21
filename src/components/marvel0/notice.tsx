"use client";
// A Note (pages-ui) that can be found by a test id and put in a live region:
// the Note itself passes no attributes on.
import type { ReactNode } from "react";
import { Note } from "@/components/pages-ui/feedback";

export function Notice({
  tone,
  role,
  testId,
  code,
  children,
}: {
  tone?: "info" | "success" | "warning";
  role?: "status" | "alert";
  testId?: string;
  /** The backend's code of the notice, for the tests and for styling. */
  code?: string;
  children: ReactNode;
}) {
  return (
    <div className="rg-notice" data-testid={testId} data-code={code}>
      <Note tone={tone} role={role}>
        {children}
      </Note>
    </div>
  );
}
