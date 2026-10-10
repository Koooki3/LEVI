"use client";
// The launch panel of /automatic: pick a job file, make a plan, read the plan
// card, launch (after a confirmation), then go to the run window. The first
// release runs dry runs only: a mode that moves a robot cannot be launched.
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { Button, Field, Select, useConfirm } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { AutomaticApiError, automaticApi, newRequestId } from "./api";
import { PlanCard } from "./launch-plan";
import { ModeChips, Note } from "./run-parts";
import type { JobEntry, LaunchPlan } from "./types";

const ERROR_KEY: Record<string, string> = {
  plan_changed: "automatic.run.launch.error.plan_changed",
  token_expired: "automatic.run.launch.error.token_expired",
  run_exists: "automatic.run.launch.error.run_exists",
  robot_busy: "automatic.run.launch.error.robot_busy",
  no_robot_adapter: "automatic.run.launch.error.no_robot_adapter",
};
const lower = (code: string) => code.replace(/^E_/, "").toLowerCase();

/** One sentence for a failed call: the catalogue's text for a known code. */
export function errorText(
  caught: unknown,
  t: (text: string) => string,
  known: Record<string, string> = ERROR_KEY,
): string {
  if (caught instanceof AutomaticApiError) {
    const key = known[lower(caught.code)];
    if (key) return t(key);
    return caught.code ? `${caught.code}: ${caught.message}` : caught.message;
  }
  return caught instanceof Error ? caught.message : String(caught);
}

export function LaunchPanel() {
  const { t } = useLocale();
  const router = useRouter();
  const { confirm, dialog } = useConfirm();
  const [jobs, setJobs] = useState<JobEntry[] | null>(null);
  const [jobsError, setJobsError] = useState("");
  const [jobId, setJobId] = useState("");
  const [plan, setPlan] = useState<LaunchPlan | null>(null);
  const [planning, setPlanning] = useState(false);
  const [launching, setLaunching] = useState(false);
  const [error, setError] = useState("");
  const [now, setNow] = useState(() => Date.now());
  // One id per plan: a second press (or a retry after a lost answer) is the
  // same launch, not another one.
  const requestId = useRef("");
  const busy = useRef(false);

  useEffect(() => {
    const controller = new AbortController();
    automaticApi
      .jobs(controller.signal)
      .then((got) => setJobs(got.jobs))
      .catch((caught) => {
        if (caught?.name !== "AbortError") setJobsError(errorText(caught, t));
      });
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => {
    if (!plan) return;
    const timer = setInterval(() => setNow(Date.now()), 5000);
    return () => clearInterval(timer);
  }, [plan]);

  const job = jobs?.find((entry) => entry.id === jobId) ?? null;

  const makePlan = async () => {
    if (!jobId || busy.current) return;
    busy.current = true;
    setPlanning(true);
    setError("");
    setPlan(null);
    try {
      const got = await automaticApi.plan(jobId);
      requestId.current = newRequestId();
      setNow(Date.now());
      setPlan(got);
    } catch (caught) {
      setError(errorText(caught, t));
    } finally {
      busy.current = false;
      setPlanning(false);
    }
  };

  const launch = async () => {
    if (!plan || busy.current) return;
    const ok = await confirm({
      title: t("automatic.run.launch.confirm.title"),
      description: t("automatic.run.launch.confirm.detail"),
      confirmLabel: t("automatic.run.launch.button"),
    });
    if (!ok || busy.current) return;
    busy.current = true;
    setLaunching(true);
    setError("");
    try {
      const got = await automaticApi.launch(plan, jobId, requestId.current);
      router.push(`/automatic/runs/${encodeURIComponent(got.run_id)}`);
    } catch (caught) {
      setError(errorText(caught, t));
      if (
        caught instanceof AutomaticApiError &&
        (caught.status === 412 || caught.status === 409)
      )
        setPlan(null);
    } finally {
      busy.current = false;
      setLaunching(false);
    }
  };

  return (
    <section className="ar-section" aria-labelledby="ar-launch-title">
      <h2 id="ar-launch-title">{t("automatic.run.launch.title")}</h2>
      <p className="ar-muted">{t("automatic.run.launch.intro")}</p>
      {jobsError && (
        <Note tone="warning" role="alert">
          {t("automatic.run.launch.jobs_failed")} ({jobsError})
        </Note>
      )}
      {jobs && jobs.length === 0 && (
        <p className="ar-muted">{t("automatic.run.launch.no_jobs")}</p>
      )}
      {jobs && jobs.length > 0 && (
        <>
          <Field label={t("automatic.run.launch.job")}>
            <Select
              value={jobId}
              onChange={(event) => {
                setJobId(event.target.value);
                setPlan(null);
                setError("");
              }}
            >
              <option value="">{t("automatic.run.launch.pick")}</option>
              {jobs.map((entry) => (
                <option key={entry.id} value={entry.id}>
                  {entry.name}
                  {entry.valid ? "" : ` (${t("automatic.run.launch.invalid")})`}
                </option>
              ))}
            </Select>
          </Field>
          {job && (
            <div className="ar-chips">
              <ModeChips reset={job.reset_mode} />
            </div>
          )}
          {job && !job.valid && (
            <Note
              tone="danger"
              role="alert"
              title={t("automatic.run.launch.invalid_job")}
            >
              <ul className="ar-checks">
                {(job.errors ?? []).map((line) => (
                  <li key={line}>{line}</li>
                ))}
              </ul>
            </Note>
          )}
          <div className="ar-actions">
            <Button
              variant="secondary"
              loading={planning}
              disabled={!job || !job.valid}
              onClick={() => void makePlan()}
            >
              {t("automatic.run.launch.plan")}
            </Button>
          </div>
        </>
      )}
      {error && (
        <Note tone="danger" role="alert">
          {error}
        </Note>
      )}
      {plan && (
        <PlanCard
          plan={plan}
          now={now}
          launching={launching}
          onLaunch={() => void launch()}
        />
      )}
      {dialog}
    </section>
  );
}
