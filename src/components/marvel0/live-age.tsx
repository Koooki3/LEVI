"use client";
// "Updated 3 s ago": how old the answer behind a card is, counted up every
// second while the tab is visible. It turns to a warning when the answer is
// older than `warnAfterMs` (the polls missed their turn) and says "no answer
// yet" before the first one, so a card never looks live when it is not.
import { useEffect, useState } from "react";
import { useLocale } from "@/components/levi-locale";
import { ago } from "@/components/pool/pool-progress";

/** Re-renders every second while the tab is visible; returns the clock. */
export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => {
      if (document.visibilityState !== "hidden") setNow(Date.now());
    }, intervalMs);
    return () => clearInterval(timer);
  }, [intervalMs]);
  return now;
}

export function LiveAge({
  lastOk,
  warnAfterMs,
  testId,
}: {
  /** Epoch milliseconds of the last good read; null before the first. */
  lastOk: number | null;
  warnAfterMs: number;
  testId?: string;
}) {
  const { t } = useLocale();
  const now = useNow();
  const ageMs = lastOk === null ? null : Math.max(0, now - lastOk);
  const late = ageMs !== null && ageMs > warnAfterMs;
  return (
    <span
      className={`rg-age${late || ageMs === null ? " rg-age--late" : ""}`}
      data-testid={testId}
      data-late={late || ageMs === null ? "true" : "false"}
    >
      {ageMs === null
        ? t("marvel0.fresh.none")
        : `${t("Updated")} ${ago(ageMs / 1000, t)}`}
    </span>
  );
}
