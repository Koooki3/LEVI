"use client";
/**
 * The header's Jobs entry: how many training pool exports/scans/pushes and
 * conversions are running, and where to follow them. It asks the two job
 * lists every 30 s while the tab is visible (and at once when it becomes
 * visible again); nothing is asked while the tab is hidden.
 */
import { useEffect, useState } from "react";
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

export function useRunningJobs(pool: boolean): JobCounts | null {
  const [counts, setCounts] = useState<JobCounts | null>(null);
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const ask = async () => {
      const [poolBody, conversionBody] = await Promise.all([
        pool ? leviApi<unknown>("pool/jobs").catch(() => null) : null,
        leviApi<unknown>("jobs").catch(() => null),
      ]);
      if (cancelled) return;
      setCounts({
        pool: countPoolJobs(poolBody),
        conversion: countConversionJobs(conversionBody),
      });
    };
    const schedule = () => {
      if (timer) clearTimeout(timer);
      timer = null;
      if (document.visibilityState === "hidden") return;
      void ask().finally(() => {
        if (!cancelled && document.visibilityState !== "hidden")
          timer = setTimeout(schedule, JOBS_POLL_MS);
      });
    };
    schedule();
    document.addEventListener("visibilitychange", schedule);
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
      document.removeEventListener("visibilitychange", schedule);
    };
  }, [pool]);
  return counts;
}

export function JobsMenu({ pool }: { pool: boolean }) {
  const { t } = useLocale();
  const router = useRouter();
  const counts = useRunningJobs(pool);
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
