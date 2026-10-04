"use client";
/**
 * The header's Jobs entry: how many training pool exports/scans/pushes and
 * conversions are running, and where to follow them. It asks the two job
 * lists on first load, when the menu opens, when the tab becomes visible
 * again and every 60 s while it is visible (never while hidden, never twice
 * at once).
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { Layers, ListChecks, Repeat } from "lucide-react";
import { Menu } from "@/components/ds";
import { leviApi } from "@/components/levi-api";
import { useLocale } from "@/components/levi-locale";
import {
  JOBS_POLL_MS,
  countConversionJobs,
  countPoolJobs,
  totalJobs,
  type JobCounts,
} from "./jobs";

export function useRunningJobs(pool: boolean): {
  counts: JobCounts | null;
  refresh: () => void;
} {
  const [counts, setCounts] = useState<JobCounts | null>(null);
  const inFlight = useRef(false);
  const alive = useRef(true);
  const refresh = useCallback(() => {
    if (inFlight.current) return;
    inFlight.current = true;
    void Promise.all([
      pool ? leviApi<unknown>("pool/jobs").catch(() => null) : null,
      leviApi<unknown>("jobs").catch(() => null),
    ])
      .then(([poolBody, conversionBody]) => {
        if (alive.current)
          setCounts({
            pool: countPoolJobs(poolBody),
            conversion: countConversionJobs(conversionBody),
          });
      })
      .finally(() => {
        inFlight.current = false;
      });
  }, [pool]);

  useEffect(() => {
    alive.current = true;
    refresh();
    let timer: ReturnType<typeof setInterval> | null = null;
    const follow = () => {
      if (timer) clearInterval(timer);
      timer =
        document.visibilityState === "hidden"
          ? null
          : setInterval(refresh, JOBS_POLL_MS);
    };
    // Back on the tab: the counts may be old, ask at once (not twice).
    const onVisibility = () => {
      follow();
      if (document.visibilityState !== "hidden") refresh();
    };
    follow();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      alive.current = false;
      if (timer) clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [refresh]);
  return { counts, refresh };
}

export function JobsMenu({ pool }: { pool: boolean }) {
  const { t } = useLocale();
  const router = useRouter();
  const { counts, refresh } = useRunningJobs(pool);
  const total = totalJobs(counts);
  const running = (n: number) =>
    n === 0 ? t("none running") : t("{n} running").replace("{n}", String(n));
  const label =
    total > 0
      ? t("Jobs: {n} running").replace("{n}", String(total))
      : t("Jobs");
  return (
    <Menu
      label={label}
      icon={ListChecks}
      iconOnly
      variant="ghost"
      align="end"
      tooltip={label}
      badge={total > 0 ? String(Math.min(total, 99)) : undefined}
      onOpenChange={(open) => open && refresh()}
      items={[
        ...(pool
          ? [
              {
                id: "pool",
                icon: Layers,
                label: `${t("Training pool jobs")} · ${running(counts?.pool ?? 0)}`,
                onSelect: () => router.push("/pool"),
              },
            ]
          : []),
        {
          id: "conversion",
          icon: Repeat,
          label: `${t("Conversions")} · ${running(counts?.conversion ?? 0)}`,
          onSelect: () => router.push("/workbench"),
        },
      ]}
    />
  );
}
