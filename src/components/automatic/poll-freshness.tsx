"use client";
// "Updated 3 s ago", counting up between polls, and "· retrying" while the last
// request failed. A page that keeps showing its last good answer through an
// outage says how old that answer is, so it is never taken for a live one.
import { useEffect, useState } from "react";
import { useLocale } from "@/components/levi-locale";
import { localDuration } from "./run-logic";

export function PollFreshness({
  lastOk,
  failures,
  error,
}: {
  lastOk: number | null;
  failures: number;
  error?: string;
}) {
  const { t } = useLocale();
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => {
      if (document.visibilityState !== "hidden") setNow(Date.now());
    }, 1000);
    return () => clearInterval(timer);
  }, []);
  if (lastOk == null && failures === 0) return null;
  const age = localDuration(Math.max(0, now - (lastOk ?? now)), t);
  const failing = failures > 0;
  const text =
    lastOk == null
      ? t("Not loaded yet · retrying")
      : t(
          failing ? "Updated {age} ago · retrying" : "Updated {age} ago",
        ).replace("{age}", age);
  return (
    <p
      className="ar-muted ar-fresh"
      data-testid="poll-freshness"
      data-failing={failing ? "true" : undefined}
      aria-live="off"
    >
      {text}
      {failing && error ? ` (${error})` : ""}
    </p>
  );
}
