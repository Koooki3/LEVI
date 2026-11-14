"use client";
import { useEffect, useState } from "react";
import { useLocale } from "@/components/levi-locale";
import { ago } from "./pool-progress";

/** "Updated 3 s ago · refreshes every 5 s", counting up between polls; when
 * the last request failed it says so in words, so figures that stopped moving
 * never look current. `onlyWhenFailing` shows nothing while polls succeed
 * (a job card already says "Last update"). The clock does not tick while the
 * tab is hidden. */
export function Freshness({
  lastOk,
  delay,
  failures,
  onlyWhenFailing = false,
  className = "pg-pool-hint",
}: {
  lastOk: number | null;
  /** The poll period in ms. */
  delay: number;
  failures: number;
  onlyWhenFailing?: boolean;
  className?: string;
}) {
  const { t, language } = useLocale();
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => {
      if (document.visibilityState !== "hidden") setNow(Date.now());
    }, 1000);
    return () => clearInterval(timer);
  }, []);
  if (onlyWhenFailing && failures === 0) return null;
  return (
    <p className={className} aria-live="off" data-failures={failures}>
      {lastOk == null
        ? t("Loading…")
        : `${t("Updated")} ${ago((now - lastOk) / 1000, t)}`}
      {" · "}
      {t("refreshes every {n} s").replace(
        "{n}",
        String(Math.round(delay / 1000)),
      )}
      {failures > 0 && (
        <span className="pg-pool-bad">
          {" · "}
          {t("last request failed")}
          {language === "zh" ? "，" : ", "}
          {t("retrying")}
        </span>
      )}
    </p>
  );
}
