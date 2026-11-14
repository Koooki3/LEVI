"use client";
import { useServerText } from "@/components/pages-ui/messages";
import { FileText, ListChecks, Play, Send, Trash2 } from "lucide-react";
import { Button, ConfirmDialog, Tooltip } from "@/components/ds";
import { Problem, RequestProblem } from "@/components/pages-ui/feedback";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocale } from "@/components/levi-locale";
import { leviRequest } from "@/components/levi-api";
import { JobDetailsDialog } from "./job-details";
import { focusRowControl } from "./focus";
import { cancelLabel, useConfirmedCancel } from "./job-panel";
import {
  ago,
  bytes,
  FINISHED_OK,
  RUNNING,
  STOPPED,
  StatusBadge,
} from "./pool-progress";
import type {
  BulkDeleteResult,
  DeleteOutput,
  DeletePlan,
  DeleteResult,
  PoolJob,
} from "./types";

function when(value: number | string | undefined | null): string {
  if (!value) return "—";
  const date =
    typeof value === "number" ? new Date(value * 1000) : new Date(value);
  return isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

/** The counts worth a glance beside a finished job's status: episodes left
 * out by data checks, failed ones, warnings ("" when there is none). */
export function jobNotes(j: PoolJob, t: (key: string) => string): string {
  const parts: string[] = [];
  const left = j.left_out ?? 0;
  const failed = j.failed ?? 0;
  const warnings = j.warning_count ?? 0;
  if (left) parts.push(`${left} ${t("left out")}`);
  if (failed) parts.push(`${failed} ${t("failed (count)")}`);
  if (warnings) parts.push(`${warnings} ${t("warnings (count)")}`);
  return parts.join(" · ");
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
    <dl className="pg-pool-facts">
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
  const serverText = useServerText();
  const askCancel = useConfirmedCancel();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [pending, setPending] = useState<Pending>(null);
  const [plans, setPlans] = useState<DeletePlan[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [details, setDetails] = useState<PoolJob | null>(null);
  const ids = useMemo(() => new Set(jobs.map((j) => j.id)), [jobs]);
  const live = (j: PoolJob) => RUNNING.has(j.status);
  // A button repeated per row names its job, so a list of buttons is not a
  // list of identical "Details".
  const named = (text: string, j: PoolJob) => `${text}: ${j.id}`;
  const failed = jobs.filter((j) => STOPPED.has(j.status));
  // Focus after a row's buttons are gone or changed: a removed record leaves
  // its neighbour's checkbox (else the list) focused, once the list has
  // refreshed without it; a resumed or cancelled job keeps its log button.
  const list = useRef<HTMLDivElement>(null);
  const afterRemoval = useRef<{ gone: string[]; next: string | null } | null>(
    null,
  );
  const [rowFocus, setRowFocus] = useState<string | null>(null);
  useEffect(() => {
    if (rowFocus === null) return;
    focusRowControl(list.current, rowFocus, "log");
    setRowFocus(null);
  }, [rowFocus]);
  const planFocus = (gone: string[]) => {
    const first = jobs.findIndex((j) => gone.includes(j.id));
    const stays = (j: PoolJob) => !gone.includes(j.id);
    const next =
      jobs.slice(first + 1).find(stays) ??
      jobs.slice(0, Math.max(0, first)).reverse().find(stays);
    afterRemoval.current = { gone, next: next?.id ?? null };
  };

  useEffect(() => {
    // A record that is gone drops out of the selection.
    setSelected((s) => new Set([...s].filter((id) => ids.has(id))));
    const plan = afterRemoval.current;
    if (plan && !plan.gone.some((id) => ids.has(id))) {
      afterRemoval.current = null;
      if (!plan.next || !focusRowControl(list.current, plan.next, "select"))
        list.current?.focus();
    }
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
      planFocus(pending.sweep ? failed.map((j) => j.id) : pending.ids);
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
    if (action === "cancel" && !(await askCancel(job))) return;
    try {
      const next = await leviRequest<PoolJob>(
        "POST",
        `pool/jobs/${encodeURIComponent(job.id)}/${action}`,
      );
      onJob(next);
      // Cancel and Resume swap this row's buttons: the log button stays.
      setRowFocus(job.id);
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
      planFocus([job.id]);
      onChanged();
    } catch (e) {
      onNotice(e instanceof Error ? e.message : String(e));
    }
  }

  const single = pending && !pending.sweep && pending.ids.length === 1;
  const title = pending?.sweep
    ? t("Clear failed, interrupted and cancelled jobs")
    : pending?.files
      ? t("Delete record and files")
      : t("Clear record");
  const label = needsForce
    ? t("Delete anyway")
    : pending?.sweep
      ? t("Clear failed, interrupted and cancelled jobs")
      : pending?.files
        ? t("Delete record and files")
        : t("Clear record");
  const allSelected = jobs.length > 0 && selected.size === jobs.length;

  return (
    <div
      ref={list}
      tabIndex={-1}
      role="group"
      aria-label={t("Recent jobs")}
      className="pg-pool-joblist"
    >
      <div className="pg-row pg-pool-bulk">
        <label className="pg-pool-check">
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
        <Button
          size="sm"
          disabled={!selected.size}
          onClick={() => void ask([...selected], false)}
        >
          {t("Clear selected")}
        </Button>
        <Button
          size="sm"
          variant="danger"
          icon={Trash2}
          disabled={!selected.size}
          onClick={() => void ask([...selected], true)}
        >
          {t("Delete selected (with files)")}
        </Button>
        <Button
          size="sm"
          disabled={!failed.length}
          onClick={() =>
            void ask(
              failed.map((j) => j.id),
              true,
              true,
            )
          }
        >
          {t("Clear failed, interrupted and cancelled jobs")} ({failed.length})
        </Button>
      </div>
      <div className="ds-table-wrap">
        <table className="ds-table ds-table--compact">
          <caption className="sr-only">{t("Recent jobs")}</caption>
          <thead>
            <tr>
              <th scope="col">
                <span className="sr-only">{t("Select")}</span>
              </th>
              <th scope="col">{t("Job")}</th>
              <th scope="col">{t("Type")}</th>
              <th scope="col">{t("Status")}</th>
              <th scope="col">{t("Output")}</th>
              <th scope="col">{t("When")}</th>
              <th scope="col">
                <span className="sr-only">{t("Actions")}</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {jobs.map((j) => (
              <tr key={j.id} data-pool-row={j.id}>
                <td>
                  <input
                    type="checkbox"
                    data-pool-focus="select"
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
                    <small className="pg-pool-muted">
                      {" "}
                      {ago(j.age_seconds, t)}
                    </small>
                  )}
                  {!live(j) && jobNotes(j, t) && (
                    <small className="pg-pool-muted"> {jobNotes(j, t)}</small>
                  )}
                </td>
                <td className="pg-pool-ellipsis">
                  <code>{jobTarget(j)}</code>
                </td>
                <td className="tabular">
                  {when(j.finished_at || j.started_at || j.planned_at)}
                </td>
                <td className="pg-pool-actions">
                  {live(j) && (
                    <Button
                      size="sm"
                      variant="ghost"
                      aria-label={named(cancelLabel(j, t), j)}
                      onClick={() => void act(j, "cancel")}
                    >
                      {cancelLabel(j, t)}
                    </Button>
                  )}
                  {j.resumable && (
                    <Button
                      size="sm"
                      variant="ghost"
                      icon={Play}
                      aria-label={named(t("Resume"), j)}
                      onClick={() => void act(j, "resume")}
                    >
                      {t("Resume")}
                    </Button>
                  )}
                  {j.kind === "export" && FINISHED_OK.has(j.status) && (
                    <Button
                      size="sm"
                      variant="ghost"
                      icon={Send}
                      aria-label={named(t("Send to remote"), j)}
                      onClick={() => onPush(j)}
                    >
                      {t("Send to remote")}
                    </Button>
                  )}
                  {!live(j) && (
                    <Button
                      size="sm"
                      variant="ghost"
                      icon={ListChecks}
                      aria-label={named(t("Details"), j)}
                      onClick={() => setDetails(j)}
                    >
                      {t("Details")}
                    </Button>
                  )}
                  <Button
                    size="sm"
                    variant="ghost"
                    icon={FileText}
                    data-pool-focus="log"
                    aria-label={named(t("View log"), j)}
                    onClick={() => onLog(j)}
                  >
                    {t("View log")}
                  </Button>
                  <Tooltip
                    content={
                      live(j)
                        ? t("Cancel the running job first")
                        : t("Remove the record only; the exported data stays")
                    }
                  >
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={live(j)}
                      aria-label={named(t("Clear record"), j)}
                      onClick={() => void clearOne(j)}
                    >
                      {t("Clear record")}
                    </Button>
                  </Tooltip>
                  {j.kind === "export" && (
                    <Tooltip
                      content={
                        live(j)
                          ? t("Cancel the running job first")
                          : t(
                              "Remove the record and the export this job produced",
                            )
                      }
                    >
                      <Button
                        size="sm"
                        variant="ghost"
                        className="pg-danger-text"
                        icon={Trash2}
                        disabled={live(j)}
                        aria-label={named(t("Delete record and files"), j)}
                        onClick={() => void ask([j.id], true)}
                      >
                        {t("Delete record and files")}
                      </Button>
                    </Tooltip>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <JobDetailsDialog job={details} onClose={() => setDetails(null)} />
      <ConfirmDialog
        open={!!pending}
        title={title}
        confirmLabel={label}
        // Removing a record or its files cannot be undone: focus starts on
        // Cancel.
        tone="danger"
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
              <p className="pg-pool-hint">
                {t(
                  "Files are deleted only for the job that produced them; a directory made by another job with the same name stays.",
                )}
              </p>
            )}
            {plans.length === 0 && !error && (
              <p className="pg-pool-muted">{t("Checking…")}</p>
            )}
            {plans.map((p) => (
              <section key={p.id} className="pg-pool-confirm-job">
                <h3>
                  <code>{p.id}</code> · <StatusBadge status={p.status} />
                </h3>
                {p.refused && (
                  <Problem
                    title={t("This job cannot be deleted")}
                    why={serverText(p.refused)}
                  />
                )}
                {pending?.files &&
                  p.outputs.map((o) => (
                    <div key={o.path}>
                      <OutputCard item={o} />
                      {o.kept_because && !o.will_delete && (
                        <p className="pg-pool-hint">
                          {serverText(o.kept_because)}
                        </p>
                      )}
                      {o.needs_force.map((n) => (
                        <p key={n} className="pg-pool-bad" role="alert">
                          {serverText(n)}
                        </p>
                      ))}
                    </div>
                  ))}
                {pending?.files && p.outputs.length === 0 && !p.refused && (
                  <p className="pg-pool-muted">
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
              <p className="pg-pool-bad" role="alert">
                {t(
                  "The folder was changed after the export. Deleting it needs this second confirmation.",
                )}
              </p>
            )}
          </>
        )}
        {error && (
          <RequestProblem action="Nothing was deleted" message={error} />
        )}
      </ConfirmDialog>
    </div>
  );
}
