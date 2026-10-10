"use client";
// The run list of /automatic: recent runs with their two labels (execution
// mode, reset mode), state and progress.
import Link from "next/link";
import { ListChecks } from "lucide-react";
import { EmptyState, Progress } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { ModeChips, Note, stateName } from "./run-parts";
import { useRunList } from "./run-poll";
import type { RunSummary } from "./types";

export function RunRows({ runs }: { runs: RunSummary[] }) {
  const { t } = useLocale();
  return (
    <ul className="ar-runs">
      {runs.map((run) => {
        const total = Math.max(run.episodes_total, 0);
        return (
          <li className="ar-run" key={run.run_id}>
            <Link
              href={`/automatic/runs/${encodeURIComponent(run.run_id)}`}
              className="ds-focus"
            >
              {run.run_id}
            </Link>
            <ModeChips execution={run.execution_mode} reset={run.reset_mode} />
            <span>{stateName(run.state, t)}</span>
            <span className="ar-run__progress">
              <Progress
                value={run.episodes_done}
                max={total || 1}
                label={t("automatic.run.list.progress")
                  .replace("{done}", String(run.episodes_done))
                  .replace("{total}", String(total))}
              />
            </span>
          </li>
        );
      })}
    </ul>
  );
}

export function RunList() {
  const { t } = useLocale();
  const poll = useRunList();
  const runs = poll.data;
  return (
    <section className="ar-section" aria-labelledby="ar-runs-title">
      <h2 id="ar-runs-title">{t("automatic.run.list.title")}</h2>
      {poll.failures > 0 && (
        <Note tone="warning" role="status">
          {t("automatic.run.list.failed")} ({poll.error})
        </Note>
      )}
      {runs === null && poll.failures === 0 && (
        <p className="ar-muted">{t("automatic.run.loading")}</p>
      )}
      {runs !== null && runs.length === 0 && (
        <EmptyState
          icon={ListChecks}
          title={t("automatic.run.list.empty")}
          description={t("automatic.run.list.empty_hint")}
        />
      )}
      {runs !== null && runs.length > 0 && <RunRows runs={runs} />}
    </section>
  );
}
