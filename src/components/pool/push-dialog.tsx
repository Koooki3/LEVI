"use client";
import { useColon } from "@/components/pages-ui/messages";
import { useServerText } from "@/components/pages-ui/messages";
import { Plus, Send, Server, Trash2, X } from "lucide-react";
import { Button, IconButton } from "@/components/ds";
import {
  EmptyLine,
  JobCard,
  RequestProblem,
} from "@/components/pages-ui/feedback";
import { useCallback, useEffect, useRef, useState } from "react";
import { useLocale } from "@/components/levi-locale";
import { leviRequest } from "@/components/levi-api";
import { useConfirmAction } from "@/components/shell/confirm";
import {
  FINISHED_OK,
  PoolJobProgress,
  RUNNING,
  StatusBadge,
  bytes,
  duration,
} from "./pool-progress";
import { JobBanner, LogDialog, useJobPolling } from "./job-panel";
import type { PoolJob, RemoteTarget } from "./types";

/** "Send to remote": pick or register a target ([user@]host:/path, SSH
 * keys only), push the finished export with rsync, follow and cancel. */
export function PushDialog({
  exportJob,
  onClose,
}: {
  exportJob: PoolJob | null;
  onClose: () => void;
}) {
  const { t } = useLocale();
  const colon = useColon();
  const serverText = useServerText();
  const confirm = useConfirmAction();
  const ref = useRef<HTMLDialogElement>(null);
  const [targets, setTargets] = useState<RemoteTarget[]>([]);
  const [target, setTarget] = useState("");
  const [dryRun, setDryRun] = useState(false);
  const [job, setJob] = useState<PoolJob | null>(null);
  const [error, setError] = useState("");
  const [adding, setAdding] = useState(false);
  const [newName, setNewName] = useState("");
  const [newSpec, setNewSpec] = useState("");
  const [newPort, setNewPort] = useState("");
  const load = useCallback(async () => {
    const value = await leviRequest<{ targets: RemoteTarget[] }>(
      "GET",
      "pool/remotes",
    );
    setTargets(value.targets);
    setTarget((current) => current || value.targets[0]?.name || "");
    if (!value.targets.length) setAdding(true);
  }, []);
  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (exportJob && !dialog.open) {
      dialog.showModal();
      setJob(null);
      setError("");
      load().catch((e) => setError(String(e)));
    } else if (!exportJob && dialog.open) dialog.close();
  }, [exportJob, load]);
  const running = !!job && RUNNING.has(job.status);
  const [logFor, setLogFor] = useState<PoolJob | null>(null);
  useJobPolling(job, setJob);
  async function act(fn: () => Promise<void>) {
    setError("");
    try {
      await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }
  const source = exportJob?.result?.dataset_path || "";
  const chosen = targets.find((x) => x.name === target);
  return (
    <dialog
      ref={ref}
      className="pg-pool-dialog ds-on-raised"
      aria-labelledby="pool-push-title"
      onClose={onClose}
    >
      <form method="dialog" className="pg-pool-dialog-head">
        <h2 id="pool-push-title">{t("Send to remote")}</h2>
        <IconButton
          type="submit"
          icon={X}
          label={t("Close")}
          tooltipPlacement="bottom"
        />
      </form>
      <p className="pg-pool-hint">
        {t("Export")}
        {colon}
        <code>{source}</code>
      </p>
      <p className="pg-pool-hint">
        {t(
          "rsync over SSH with your SSH key (no passwords). The host key must already be in known_hosts; an interrupted push resumes when you send it again.",
        )}
      </p>
      <fieldset className="pg-pool-targets">
        <legend>{t("Remote target")}</legend>
        {targets.map((x) => (
          <label key={x.name} className="pg-pool-check">
            <input
              type="radio"
              name="pool-target"
              checked={target === x.name}
              onChange={() => setTarget(x.name)}
            />
            <span className="grow">
              <strong>{x.name}</strong> <code>{x.display}</code>
            </span>
            <Button
              size="sm"
              variant="ghost"
              className="pg-danger-text"
              icon={Trash2}
              aria-label={`${t("Delete")}${colon}${x.name}`}
              onClick={() =>
                void act(async () => {
                  if (
                    !(await confirm({
                      title: `${t("Forget target")} ${x.name}?`,
                      confirmLabel: t("Forget target"),
                      tone: "danger",
                    }))
                  )
                    return;
                  await leviRequest(
                    "DELETE",
                    `pool/remotes/${encodeURIComponent(x.name)}`,
                  );
                  if (target === x.name) setTarget("");
                  await load();
                })
              }
            >
              {t("Delete")}
            </Button>
          </label>
        ))}
        {!targets.length && (
          <EmptyLine icon={Server}>{t("No remote targets yet.")}</EmptyLine>
        )}
        {adding ? (
          <form
            className="pg-pool-fields"
            onSubmit={(e) => {
              e.preventDefault();
              void act(async () => {
                await leviRequest(
                  "PUT",
                  `pool/remotes/${encodeURIComponent(newName)}`,
                  {
                    spec: newSpec,
                    ...(newPort ? { port: Number(newPort) } : {}),
                  },
                );
                setTarget(newName);
                setNewName("");
                setNewSpec("");
                setNewPort("");
                setAdding(false);
                await load();
              });
            }}
          >
            <label>
              <span>{t("Name")}</span>
              <input
                className="ds-input ds-focus"
                required
                pattern="[A-Za-z0-9][A-Za-z0-9._\-]{0,63}"
                placeholder="gpu-server"
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
              />
            </label>
            <label className="wide">
              <span>
                {t("[user@]host:/path (host may be an ~/.ssh/config alias)")}
              </span>
              <input
                className="ds-input ds-focus"
                required
                placeholder="wk@gpu-server:/data/datasets"
                value={newSpec}
                autoComplete="off"
                spellCheck={false}
                onChange={(e) => setNewSpec(e.target.value)}
              />
            </label>
            <label>
              <span>{t("Port (optional)")}</span>
              <input
                className="ds-input ds-focus"
                type="number"
                min={1}
                max={65535}
                value={newPort}
                onChange={(e) => setNewPort(e.target.value)}
              />
            </label>
            <div className="pg-row wide">
              <Button type="submit">{t("Save target")}</Button>
              {targets.length > 0 && (
                <Button variant="ghost" onClick={() => setAdding(false)}>
                  {t("Cancel")}
                </Button>
              )}
            </div>
          </form>
        ) : (
          <Button
            size="sm"
            variant="ghost"
            icon={Plus}
            className="pg-align-start"
            onClick={() => setAdding(true)}
          >
            {t("Add a target")}
          </Button>
        )}
      </fieldset>
      {chosen && (
        <p className="pg-pool-hint">
          {t("Destination")}
          {colon}
          <code>
            {chosen.display?.split(" ")[0]}/{source.split("/").pop()}/
          </code>
        </p>
      )}
      <label className="pg-pool-check">
        <input
          type="checkbox"
          checked={dryRun}
          onChange={() => setDryRun(!dryRun)}
        />
        <span>{t("Dry run (list only, send nothing)")}</span>
      </label>
      <div className="pg-row">
        <Button
          variant="primary"
          icon={Send}
          disabled={!target || !exportJob || running}
          onClick={() =>
            void act(async () => {
              setJob(
                await leviRequest<PoolJob>("POST", "pool/push", {
                  target,
                  export_job: exportJob?.id,
                  dry_run: dryRun,
                }),
              );
            })
          }
        >
          {t("Send")}
        </Button>
        {running && job && (
          <Button
            icon={X}
            onClick={() =>
              void act(async () => {
                await leviRequest(
                  "POST",
                  `pool/jobs/${encodeURIComponent(job.id)}/cancel`,
                );
              })
            }
          >
            {t("Cancel transfer")}
          </Button>
        )}
      </div>
      {error && (
        <RequestProblem action="The transfer did not start" message={error} />
      )}
      {job && (
        <JobCard
          label={t("Send to remote")}
          status={<StatusBadge status={job.status} />}
          title={<code>{job.destination}</code>}
        >
          <PoolJobProgress job={job} />
          <JobBanner job={job} onJob={setJob} onLog={setLogFor} />
          {FINISHED_OK.has(job.status) && (
            <p className="pg-pool-hint">
              {job.dry_run ? t("Dry run finished") : t("Sent")}:{" "}
              {bytes(job.result?.bytes)} · {duration(job.result?.seconds)}
            </p>
          )}
          {job.error && job.status === "cancelled" && (
            <pre className="pg-code">{serverText(job.error)}</pre>
          )}
        </JobCard>
      )}
      <LogDialog job={logFor} onClose={() => setLogFor(null)} />
    </dialog>
  );
}
