"use client";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useLocale } from "@/components/levi-locale";
import { leviRequest } from "@/components/levi-api";
import { ConfirmDialog } from "./confirm-dialog";
import { ago, bytes, RUNNING, StatusBadge } from "./pool-progress";
import type {
  BulkDeleteResult,
  DeleteOutput,
  DeletePlan,
  DeleteResult,
  PoolJob,
} from "./types";

const STOPPED = new Set(["failed", "interrupted", "cancelled"]);

function when(value: number | string | undefined | null): string {
  if (!value) return "—";
  const date =
    typeof value === "number" ? new Date(value * 1000) : new Date(value);
  return isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

/** What a job produced, for the list's path column. */
export function jobTarget(j: PoolJob): string {
  if (j.kind === "push") return j.destination || "";
  if (j.kind === "export") return j.result?.dataset_path || j.target || "";
  return "";
}

/** One export directory (or partial) as the confirm dialog shows it. */
function OutputCard({ item }: { item: DeleteOutput }) {
  const { t } = useLocale();
  return (
    <dl className="levi-pool-facts">
      <dt>{item.role === "partial" ? t("Unfinished output") : t("Export")}</dt>
      <dd>
        <code>{item.path}</code>
      </dd>
      <dt>{t("Size")}</dt>
      <dd>{bytes(item.bytes)}</dd>
      {item.role === "output" && (
        <>
          <dt>{t("Episodes")}</dt>
          <dd>{item.episodes ?? "—"}</dd>
          <dt>{t("Format")}</dt>
          <dd>{item.format ?? "—"}</dd>
          <dt>{t("Created")}</dt>
          <dd>{when(item.created_at)}</dd>
          <dt>{t("Sent to remote")}</dt>
          <dd>
            {item.pushed.length
              ? item.pushed
                  .map((p) => `${p.remote ?? "?"} (${t(p.status)})`)
                  .join(", ")
              : t("no")}
          </dd>
        </>
      )}
      <dt>{t("Produced by")}</dt>
      <dd>
        {item.owner ? <code>{item.owner}</code> : t("unknown")}
        {item.will_delete
          ? ` · ${t("this job: it will be deleted")}`
          : item.owner && !item.owned
            ? ` · ${t("another job: it stays")}`
            : ""}
      </dd>
    </dl>
  );
}

type Pending = { ids: string[]; files: boolean; sweep?: boolean } | null;

/** The recent-jobs table with per-job and bulk actions: clear the record, or
 * delete the record and the export that job produced (never one another job
 * produced, never while running). The server enforces every rule; this
 * shows what will go and asks first, with Cancel as the default. */
export function RecentJobs({
  jobs,
  onChanged,
  onPush,
  onJob,
  onLog,
  onNotice,
}: {
  jobs: PoolJob[];
  onChanged: () => void;
  onPush: (job: PoolJob) => void;
  onJob: (job: PoolJob) => void;
  onLog: (job: PoolJob) => void;
  onNotice: (text: string) => void;
}) {
  const { t } = useLocale();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [pending, setPending] = useState<Pending>(null);
  const [plans, setPlans] = useState<DeletePlan[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const ids = useMemo(() => new Set(jobs.map((j) => j.id)), [jobs]);
  const live = (j: PoolJob) => RUNNING.has(j.status);
  const failed = jobs.filter((j) => STOPPED.has(j.status));

  useEffect(() => {
    // A record that is gone drops out of the selection.
    setSelected((s) => new Set([...s].filter((id) => ids.has(id))));
  }, [ids]);

  const ask = useCallback(
    async (target: string[], files: boolean, sweep = false) => {
      setError("");
      setPlans([]);
      setPending({ ids: target, files, sweep });
      if (sweep) return;
      try {
        const next = await Promise.all(
          target.map((id) =>
            leviRequest<DeletePlan>(
              "GET",
              `pool/jobs/${encodeURIComponent(id)}/delete-preview?files=${files}`,
            ),
          ),
        );
        setPlans(next);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    },
    [],
  );

  const needsForce = plans.some((p) => p.needs_force);
  const refused = plans.filter((p) => p.refused);
  const freed = plans.reduce((n, p) => n + (p.refused ? 0 : p.freed_bytes), 0);

  async function confirm() {
    if (!pending) return;
    setBusy(true);
    setError("");
    try {
      let message = "";
      if (pending.sweep) {
        const r = await leviRequest<BulkDeleteResult>(
          "POST",
          "pool/jobs/clear-failed",
        );
        message = `${t("Cleared")} ${r.cleared ?? r.results.length} ${t("jobs")} · ${t("Freed")} ${bytes(r.freed_bytes)}`;
      } else if (pending.ids.length === 1) {
        const id = pending.ids[0];
        const r = await leviRequest<DeleteResult>(
          "DELETE",
          `pool/jobs/${encodeURIComponent(id)}?files=${pending.files}&force=${needsForce}`,
        );
        if (r.errors.length) throw new Error(r.errors.join("; "));
        message = `${t("Cleared")} ${id} · ${t("Freed")} ${bytes(r.freed_bytes)}`;
      } else {
        const r = await leviRequest<BulkDeleteResult>(
          "POST",
          "pool/jobs/delete",
          { ids: pending.ids, files: pending.files, force: needsForce },
        );
        const skipped = r.refused.length
          ? ` · ${r.refused.length} ${t("refused")}: ${r.refused.map((x) => x.reason).join("; ")}`
          : "";
        message = `${t("Cleared")} ${r.results.length} ${t("jobs")} · ${t("Freed")} ${bytes(r.freed_bytes)}${skipped}`;
      }
      onNotice(message);
      setPending(null);
      setSelected(new Set());
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function act(job: PoolJob, action: "resume" | "cancel") {
    try {
      const next = await leviRequest<PoolJob>(
        "POST",
        `pool/jobs/${encodeURIComponent(job.id)}/${action}`,
      );
      onJob(next);
      onChanged();
    } catch (e) {
      onNotice(e instanceof Error ? e.message : String(e));
    }
  }

  async function clearOne(job: PoolJob) {
    try {
      const r = await leviRequest<DeleteResult>(
        "DELETE",
        `pool/jobs/${encodeURIComponent(job.id)}?files=false`,
      );
      onNotice(
        `${t("Cleared")} ${job.id} · ${t("Freed")} ${bytes(r.freed_bytes)}`,
      );
      onChanged();
    } catch (e) {
      onNotice(e instanceof Error ? e.message : String(e));
    }
  }

  const single = pending && !pending.sweep && pending.ids.length === 1;
  const title = pending?.sweep
    ? t("Clear all failed and interrupted jobs")
    : pending?.files
      ? t("Delete record and files")
      : t("Clear record");
  const label = needsForce
    ? t("Delete anyway")
    : pending?.sweep
      ? t("Clear failed jobs")
      : pending?.files
        ? t("Delete record and files")
        : t("Clear record");
  const allSelected = jobs.length > 0 && selected.size === jobs.length;

  return (
    <div className="levi-pool-joblist">
      <div className="levi-row levi-pool-bulk">
        <label className="levi-pool-check">
          <input
            type="checkbox"
            checked={allSelected}
            onChange={() =>
              setSelected(
                allSelected ? new Set() : new Set(jobs.map((j) => j.id)),
              )
            }
            aria-label={t("Select all jobs")}
          />
          <span>{t("Select all")}</span>
        </label>
        <button
          type="button"
          className="levi-secondary"
          disabled={!selected.size}
          onClick={() => void ask([...selected], false)}
        >
          {t("Clear selected")}
        </button>
        <button
          type="button"
          className="levi-pool-danger"
          disabled={!selected.size}
          onClick={() => void ask([...selected], true)}
        >
          {t("Delete selected (with files)")}
        </button>
        <button
          type="button"
          className="levi-secondary"
          disabled={!failed.length}
          onClick={() =>
            void ask(
              failed.map((j) => j.id),
              true,
              true,
            )
          }
        >
          {t("Clear all failed and interrupted jobs")} ({failed.length})
        </button>
      </div>
      <table className="levi-table">
        <tbody>
          {jobs.map((j) => (
            <tr key={j.id}>
              <td>
                <input
                  type="checkbox"
                  checked={selected.has(j.id)}
                  aria-label={`${t("Select")} ${j.id}`}
                  onChange={() =>
                    setSelected((s) => {
                      const next = new Set(s);
                      if (!next.delete(j.id)) next.add(j.id);
                      return next;
                    })
                  }
                />
              </td>
              <td>
                <code>{j.id}</code>
              </td>
              <td>{t(j.kind)}</td>
              <td>
                <StatusBadge status={j.status} />
                {live(j) && j.age_seconds !== undefined && (
                  <small className="levi-pool-muted">
                    {" "}
                    {ago(j.age_seconds, t)}
                  </small>
                )}
              </td>
              <td className="levi-pool-ellipsis">
                <code title={jobTarget(j)}>{jobTarget(j)}</code>
              </td>
              <td>{when(j.finished_at || j.started_at || j.planned_at)}</td>
              <td className="levi-pool-actions">
                {live(j) && (
                  <button
                    type="button"
                    className="levi-pool-link"
                    onClick={() => void act(j, "cancel")}
                  >
                    {t("Cancel")}
                  </button>
                )}
                {j.resumable && (
                  <button
                    type="button"
                    className="levi-pool-link"
                    onClick={() => void act(j, "resume")}
                  >
                    {t("Resume")}
                  </button>
                )}
                {j.kind === "export" &&
                  (j.status === "done" || j.status === "done_with_errors") && (
                    <button
                      type="button"
                      className="levi-pool-link"
                      onClick={() => onPush(j)}
                    >
                      {t("Send to remote")}
                    </button>
                  )}
                <button
                  type="button"
                  className="levi-pool-link"
                  onClick={() => onLog(j)}
                >
                  {t("View log")}
                </button>
                <button
                  type="button"
                  className="levi-pool-link"
                  disabled={live(j)}
                  title={
                    live(j)
                      ? t("Cancel the running job first")
                      : t("Remove the record only; the exported data stays")
                  }
                  onClick={() => void clearOne(j)}
                >
                  {t("Clear record")}
                </button>
                {j.kind === "export" && (
                  <button
                    type="button"
                    className="levi-pool-link levi-pool-danger-link"
                    disabled={live(j)}
                    title={
                      live(j)
                        ? t("Cancel the running job first")
                        : t(
                            "Remove the record and the export this job produced",
                          )
                    }
                    onClick={() => void ask([j.id], true)}
                  >
                    {t("Delete record and files")}
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <ConfirmDialog
        open={!!pending}
        title={title}
        confirmLabel={label}
        danger={!!pending?.files || !!pending?.sweep}
        busy={busy}
        disabled={
          !pending?.sweep &&
          (plans.length === 0 || refused.length === plans.length)
        }
        onConfirm={() => void confirm()}
        onCancel={() => setPending(null)}
      >
        {pending?.sweep ? (
          <p>
            {t(
              "Removes the records of failed, interrupted and cancelled jobs and their unfinished output folders. Finished exports are never touched.",
            )}{" "}
            ({pending.ids.length})
          </p>
        ) : (
          <>
            {!pending?.files && (
              <p>
                {t(
                  "Removes only the job records and logs. The exported data stays.",
                )}
              </p>
            )}
            {pending?.files && (
              <p className="levi-pool-hint">
                {t(
                  "Files are deleted only for the job that produced them; a directory made by another job with the same name stays.",
                )}
              </p>
            )}
            {plans.length === 0 && !error && (
              <p className="levi-pool-muted">{t("Checking…")}</p>
            )}
            {plans.map((p) => (
              <section key={p.id} className="levi-pool-confirm-job">
                <h3>
                  <code>{p.id}</code> · <StatusBadge status={p.status} />
                </h3>
                {p.refused && (
                  <p className="levi-error" role="alert">
                    {t(p.refused)}
                  </p>
                )}
                {pending?.files &&
                  p.outputs.map((o) => (
                    <div key={o.path}>
                      <OutputCard item={o} />
                      {o.kept_because && !o.will_delete && (
                        <p className="levi-pool-hint">{t(o.kept_because)}</p>
                      )}
                      {o.needs_force.map((n) => (
                        <p key={n} className="levi-pool-bad" role="alert">
                          {t(n)}
                        </p>
                      ))}
                    </div>
                  ))}
                {pending?.files && p.outputs.length === 0 && !p.refused && (
                  <p className="levi-pool-muted">
                    {t("This job has no files on disk.")}
                  </p>
                )}
              </section>
            ))}
            {single || plans.length > 0 ? (
              <p>
                <strong>{t("Frees about")}</strong> {bytes(freed)}
                {refused.length > 0 &&
                  ` · ${refused.length} ${t("cannot be deleted")}`}
              </p>
            ) : null}
            {needsForce && (
              <p className="levi-pool-bad" role="alert">
                {t(
                  "The folder was changed after the export. Deleting it needs this second confirmation.",
                )}
              </p>
            )}
          </>
        )}
        {error && (
          <p className="levi-error" role="alert">
            {t(error)}
          </p>
        )}
      </ConfirmDialog>
    </div>
  );
}
