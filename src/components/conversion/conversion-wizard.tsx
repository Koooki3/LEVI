"use client";
import { useMemo, useState } from "react";
import Link from "next/link";
import { T, useLocale } from "@/components/levi-locale";
import { leviApi } from "@/components/levi-api";
import { InputReportView } from "./input-report";
import { JobProgress } from "./job-progress";
import { OptionsForm } from "./options-form";
import { TargetCards } from "./target-cards";
import type {
  ConversionOptions,
  InputReport,
  Job,
  Solution,
  TargetCompatibility,
} from "./types";

const INITIAL: ConversionOptions = {
  fps: 10,
  source_fps: 30,
  timing: "resample",
  filter_static: true,
  workers: null,
  keep_intermediates: false,
  exclude_demos: [],
  target: "lerobot_v21",
  target_options: {},
};

/** Inspect → requirement checklist → export cards → options → plan → run.
 * The page owns job polling and passes the job list in. */
export function ConversionWizard({
  jobs,
  refresh,
  available,
  source,
  onSourceChange,
}: {
  jobs: Job[];
  refresh: () => Promise<void>;
  available: boolean;
  source: string;
  onSourceChange: (value: string) => void;
}) {
  const { t } = useLocale();
  const [inspectId, setInspectId] = useState<string | null>(null);
  const [options, setOptions] = useState<ConversionOptions>(INITIAL);
  const [extra, setExtra] = useState("{}");
  const [target, setTarget] = useState<TargetCompatibility | null>(null);
  const [output, setOutput] = useState("");
  const [plan, setPlan] = useState<Job | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);

  const inspectJob = jobs.find((j) => j.id === inspectId) ?? null;
  const runJob = jobs.find((j) => j.id === runId) ?? null;
  const report = useMemo(
    () =>
      (inspectJob?.result as { report?: InputReport } | undefined)?.report ??
      null,
    [inspectJob],
  );
  const inspecting =
    inspectJob !== null &&
    (inspectJob.status === "queued" || inspectJob.status === "running");

  function extraOptions(): Record<string, unknown> {
    const parsed = JSON.parse(extra || "{}") as unknown;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed))
      throw new Error(t("Advanced options must be a JSON object"));
    return parsed as Record<string, unknown>;
  }

  async function run(fn: () => Promise<void>) {
    setError("");
    setBusy(true);
    try {
      await fn();
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  function inspect(next: ConversionOptions = options) {
    setTarget(null);
    setPlan(null);
    void run(async () => {
      const job = await leviApi<Job>("convert/inspect", {
        source,
        options: {
          ...extraOptions(),
          fps: next.fps,
          source_fps: next.source_fps,
          exclude_demos: next.exclude_demos,
          target_options: next.target_options,
        },
      });
      setInspectId(job.id);
    });
  }

  function choose(card: TargetCompatibility) {
    setTarget(card);
    setPlan(null);
    setOptions((current) => ({
      ...current,
      ...(card.defaults as Partial<ConversionOptions>),
      target: card.target,
    }));
  }

  function applySolution(solution: Solution) {
    if (solution.link === "label") {
      void run(async () => {
        const entry = await leviApi<{
          id: string;
          kind?: string;
          view_status?: string;
        }>("catalog", { path: source });
        if (entry.kind === "raw" && entry.view_status !== "ready") {
          setNotice(
            t(
              "Building a browsing view of this capture. Open it from Local datasets when it is ready, then click the outcome dot beside each episode.",
            ),
          );
          return;
        }
        window.location.href = `/${entry.id}`;
      });
      return;
    }
    const next = {
      ...options,
      ...(solution.options as Partial<ConversionOptions>),
    };
    setOptions(next);
    // Exclusions and label semantics change the report: inspect again.
    inspect(next);
  }

  function preview() {
    void run(async () => {
      const job = await leviApi<Job>("jobs/plan", {
        stage: "pipeline",
        source,
        fps: options.fps,
        source_fps: options.source_fps,
        options: { ...extraOptions(), ...options },
        ...(output.trim() ? { output: output.trim() } : {}),
      });
      setPlan(job);
    });
  }

  return (
    <div>
      {error && (
        <p className="levi-error" role="alert">
          {t(error)}
        </p>
      )}
      {notice && <p className="levi-status warn mt-3">{notice}</p>}
      <form
        className="levi-row mt-4"
        onSubmit={(e) => {
          e.preventDefault();
          inspect();
        }}
      >
        <input
          className="levi-input grow"
          aria-label={t("Input directory")}
          placeholder={t(
            "Raw capture (task/demo_NNNN folders) or a LeRobot dataset",
          )}
          value={source}
          required
          onChange={(e) => {
            onSourceChange(e.target.value);
            setInspectId(null);
            setTarget(null);
            setPlan(null);
            setOptions(INITIAL);
          }}
        />
        <button
          className="levi-primary"
          disabled={busy || inspecting || !available}
        >
          <T>{inspecting ? "Inspecting…" : "1 · Inspect input"}</T>
        </button>
      </form>
      <details className="mt-3">
        <summary className="cursor-pointer text-xs">
          <T>Advanced conversion options</T>
        </summary>
        <p className="my-2 text-xs">
          <T>
            JSON options: camera mapping, task mapping, excluded demo paths,
            orientation, action mode and quality thresholds.
          </T>{" "}
          <Link href="/guide">
            <T>Guide</T> ↗
          </Link>
        </p>
        <textarea
          aria-label={t("Conversion options JSON")}
          className="levi-input w-full font-mono text-xs"
          rows={6}
          value={extra}
          onChange={(e) => setExtra(e.target.value)}
        />
      </details>

      {inspectJob && inspecting && <JobProgress job={inspectJob} />}
      {inspectJob?.status === "failed" && (
        <p className="levi-error" role="alert">
          {t(inspectJob.error || "Inspection failed")}
        </p>
      )}
      {report && (
        <>
          <h3 className="mt-6">
            <T>2 · Input requirements</T>
          </h3>
          <InputReportView report={report} />
          <h3 className="mt-6">
            <T>3 · Choose an export</T>
          </h3>
          <TargetCards
            targets={report.targets}
            selected={target?.target ?? null}
            onSelect={choose}
            onSolution={applySolution}
          />
        </>
      )}
      {report && target && (
        <>
          <h3 className="mt-6">
            <T>4 · Options</T>
          </h3>
          <OptionsForm
            report={report}
            options={options}
            onChange={(next) => {
              setOptions(next);
              setPlan(null);
            }}
            output={output}
            onOutputChange={(value) => {
              setOutput(value);
              setPlan(null);
            }}
          />
          <button
            className="levi-primary mt-4"
            disabled={busy}
            onClick={preview}
          >
            <T>5 · Review plan</T>
          </button>
        </>
      )}
      {plan && (
        <div className="mt-6">
          <p>
            <T>New output</T>: <code>{plan.output}</code>
          </p>
          <pre>
            {JSON.stringify(
              {
                target: plan.options?.target,
                timing: plan.options?.timing,
                fps: plan.options?.fps,
                filter_static: plan.options?.filter_static,
                target_options: plan.options?.target_options,
                excluded: (plan.options?.exclude_demos as string[] | undefined)
                  ?.length,
                human_outcome_labels: Object.keys(
                  (plan.options?.outcome_labels as object | undefined) ?? {},
                ).length,
              },
              null,
              2,
            )}
          </pre>
          <button
            className="levi-primary mt-3"
            disabled={busy}
            onClick={() =>
              void run(async () => {
                await leviApi(`jobs/${plan.id}/run`, {});
                setRunId(plan.id);
                setPlan(null);
              })
            }
          >
            <T>6 · Run conversion</T> ↗
          </button>
        </div>
      )}
      {runJob && (
        <div className="mt-6">
          <p>
            <span
              className={`levi-status ${
                runJob.status === "failed"
                  ? "fail"
                  : runJob.status === "succeeded"
                    ? "pass"
                    : ""
              }`}
            >
              {t(runJob.status)}
            </span>
          </p>
          <JobProgress job={runJob} />
          {runJob.error && (
            <p className="levi-error" role="alert">
              {t(runJob.error)}
            </p>
          )}
          {runJob.dataset && (
            <Link className="levi-primary" href={`/${runJob.dataset}`}>
              <T>Review converted dataset</T> ↗
            </Link>
          )}
        </div>
      )}
    </div>
  );
}
