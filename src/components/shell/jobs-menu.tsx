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
import { Menu, useToast } from "@/components/ds";
import { leviApi } from "@/components/levi-api";
import { useLocale } from "@/components/levi-locale";
import {
  JOBS_ACTIVE_POLL_MS,
  JOBS_POLL_MS,
  conversionEntries,
  countConversionJobs,
  countPoolJobs,
  finishedSince,
  poolEntries,
  runningFraction,
  totalJobs,
  type JobCounts,
  type JobEntry,
  type JobOutcome,
} from "./jobs";

export function useRunningJobs(
  pool: boolean,
  onFinished?: (
    finished: Array<{ entry: JobEntry; outcome: JobOutcome }>,
  ) => void,
): {
  counts: JobCounts | null;
  entries: JobEntry[];
  refresh: () => void;
} {
  const [counts, setCounts] = useState<JobCounts | null>(null);
  const [entries, setEntries] = useState<JobEntry[]>([]);
  const inFlight = useRef(false);
  const alive = useRef(true);
  const previous = useRef<Map<string, JobEntry> | null>(null);
  const finishedRef = useRef(onFinished);
  finishedRef.current = onFinished;
  const refresh = useCallback(() => {
    if (inFlight.current) return;
    inFlight.current = true;
    void Promise.all([
      pool ? leviApi<unknown>("pool/jobs").catch(() => null) : null,
      leviApi<unknown>("jobs").catch(() => null),
    ])
      .then(([poolBody, conversionBody]) => {
        if (!alive.current) return;
        const next = [
          ...poolEntries(poolBody),
          ...conversionEntries(conversionBody),
        ];
        // An answer that failed (null) leaves the old state: a job is not
        // "finished" because the list could not be read.
        const answered =
          (pool ? poolBody !== null : true) && conversionBody !== null;
        if (answered) {
          const done = finishedSince(previous.current, next);
          previous.current = new Map(next.map((e) => [e.key, e]));
          if (done.length) finishedRef.current?.(done);
          setEntries(next);
        }
        setCounts({
          pool: countPoolJobs(poolBody),
          conversion: countConversionJobs(conversionBody),
        });
      })
      .finally(() => {
        inFlight.current = false;
      });
  }, [pool]);

  const active = totalJobs(counts) > 0;

  // First load, and back on the tab: the counts may be old, ask at once
  // (never twice).
  useEffect(() => {
    alive.current = true;
    refresh();
    const onVisibility = () => {
      if (document.visibilityState !== "hidden") refresh();
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      alive.current = false;
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [refresh]);

  // While the tab is visible: every minute, every few seconds while a job
  // runs (so its result is announced soon); nothing while hidden.
  useEffect(() => {
    let timer: ReturnType<typeof setInterval> | null = null;
    const follow = () => {
      if (timer) clearInterval(timer);
      timer =
        document.visibilityState === "hidden"
          ? null
          : setInterval(refresh, active ? JOBS_ACTIVE_POLL_MS : JOBS_POLL_MS);
    };
    follow();
    document.addEventListener("visibilitychange", follow);
    return () => {
      if (timer) clearInterval(timer);
      document.removeEventListener("visibilitychange", follow);
    };
  }, [refresh, active]);
  return { counts, entries, refresh };
}

const percent = (fraction: number) => `${Math.round(fraction * 100)}%`;

export function JobsMenu({ pool }: { pool: boolean }) {
  const { t, language } = useLocale();
  const router = useRouter();
  const toast = useToast();
  // "Training pool export: finished" / "训练池导出：已完成".
  const colon = language === "zh" ? "：" : ": ";
  const { counts, entries, refresh } = useRunningJobs(pool, (finished) => {
    for (const { entry, outcome } of finished) {
      const page = entry.kind === "pool" ? "/pool" : "/workbench";
      toast.show({
        tone:
          outcome === "success"
            ? "success"
            : outcome === "warning"
              ? "warning"
              : "danger",
        title:
          outcome === "success"
            ? `${t(entry.what)}${colon}${t("finished")}`
            : outcome === "warning"
              ? `${t(entry.what)}${colon}${t("finished with errors")}`
              : `${t(entry.what)}${colon}${t("failed")}`,
        description: entry.name || undefined,
        action: { label: t("Show the job"), onClick: () => router.push(page) },
      });
    }
  });
  const total = totalJobs(counts);
  const running = (n: number, kind: "pool" | "conversion") => {
    if (n === 0) return t("none running");
    const base = t("{n} running").replace("{n}", String(n));
    const fraction = runningFraction(entries, kind);
    return fraction === null ? base : `${base} · ${percent(fraction)}`;
  };
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
                label: `${t("Training pool jobs")} · ${running(counts?.pool ?? 0, "pool")}`,
                onSelect: () => router.push("/pool"),
              },
            ]
          : []),
        {
          id: "conversion",
          icon: Repeat,
          label: `${t("Conversions")} · ${running(counts?.conversion ?? 0, "conversion")}`,
          onSelect: () => router.push("/workbench"),
        },
      ]}
    />
  );
}
