"use client";
import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { ArrowUpRight, Trash2 } from "lucide-react";
import {
  Button,
  Checkbox,
  ConfirmDialog,
  Dialog,
  Icon,
  useToast,
} from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { leviRequest } from "@/components/levi-api";
import type { LiveLink } from "./embedding";

export type ManagementRequest = <T>(
  method: "POST" | "DELETE",
  path: string,
  body: unknown,
) => Promise<T>;

export interface DeletionPlan {
  confirmation: string;
  files: number;
  bytes: number;
  paths: string[];
}

export type LiveDeletionTarget =
  | {
      kind: "session";
      root: string;
      group: string;
      task_folder: string;
      session_id: string;
    }
  | { kind: "dataset"; name: string };

function deletionRequest(target: LiveDeletionTarget, deleteFiles: boolean) {
  if (target.kind === "session") {
    return {
      path: "live/sessions",
      body: {
        root: target.root,
        group: target.group,
        task_folder: target.task_folder,
        session_id: target.session_id,
      },
    };
  }
  return {
    path: `live/datasets/${encodeURIComponent(target.name)}`,
    body: { delete_files: deleteFiles },
  };
}

export function LiveDeleteButton({
  target,
  title,
  blocked,
  onChanged,
  request = leviRequest,
}: {
  target: LiveDeletionTarget | null;
  title: string;
  blocked?: string;
  onChanged?: () => void;
  request?: ManagementRequest;
}) {
  const { t } = useLocale();
  const toast = useToast();
  const [choose, setChoose] = useState(false);
  const [deleteFiles, setDeleteFiles] = useState(false);
  const [plan, setPlan] = useState<DeletionPlan | null>(null);
  const [busy, setBusy] = useState(false);
  const locked = useRef(false);
  const [error, setError] = useState("");
  const label =
    target?.kind === "dataset" ? "Delete pipeline" : "Delete session";
  const reason =
    blocked ||
    (!target
      ? t("Deletion is unavailable: the session identity is missing.")
      : "");
  const consequence =
    target?.kind === "dataset"
      ? deleteFiles
        ? "The pipeline record, mirrored data and viewer files will be deleted. Original rollout files are kept; delete individual source episodes from the local viewer when needed."
        : "Only the pipeline record will be deleted. Mirrored data, viewer files and original rollout files are kept."
      : "Only the evaluation session record will be deleted. Its episodes, annotation pipeline and original rollout files are kept.";

  async function preview() {
    if (!target || locked.current) return;
    locked.current = true;
    setBusy(true);
    setError("");
    const call = deletionRequest(target, deleteFiles);
    try {
      const result = await request<DeletionPlan>(
        "POST",
        `${call.path}/deletion-plan`,
        call.body,
      );
      setPlan(result);
      setChoose(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      locked.current = false;
      setBusy(false);
    }
  }

  async function apply() {
    if (!target || !plan || locked.current) return;
    locked.current = true;
    setBusy(true);
    setError("");
    const call = deletionRequest(target, deleteFiles);
    try {
      const result = await request<{ cleanup_pending?: boolean }>(
        "DELETE",
        call.path,
        {
          ...call.body,
          confirmation: plan.confirmation,
        },
      );
      setPlan(null);
      if (result?.cleanup_pending) {
        toast.show({
          title: t("Pipeline removed, but some files still need cleanup."),
          description: title,
          tone: "warning",
          duration: null,
        });
      }
      onChanged?.();
    } catch (e) {
      setPlan(null);
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      locked.current = false;
      setBusy(false);
    }
  }

  return (
    <>
      <Button
        variant="ghost"
        size="sm"
        icon={Trash2}
        disabled={!!reason}
        loading={busy}
        aria-label={`${t(label)}: ${title}`}
        onClick={() => {
          setError("");
          if (target?.kind === "dataset") {
            setDeleteFiles(false);
            setChoose(true);
          } else void preview();
        }}
      >
        {t(label)}
      </Button>
      {reason && <span className="pg-live-action-note">{reason}</span>}
      {error && !choose && (
        <span className="pg-live-action-note pg-live-bad" role="alert">
          {error}
        </span>
      )}
      <Dialog
        open={choose}
        title={`${t("Delete pipeline")}: ${title}`}
        onClose={() => {
          if (!locked.current) setChoose(false);
        }}
        hideClose={busy}
        closeOnScrim={!busy}
        footer={
          <>
            <Button disabled={busy} onClick={() => setChoose(false)}>
              {t("Cancel")}
            </Button>
            <Button loading={busy} onClick={() => void preview()}>
              {t("Preview deletion")}
            </Button>
          </>
        }
      >
        <p>
          {t(
            "Remove this pipeline from the live list. Original rollout files are kept.",
          )}
        </p>
        <Checkbox
          checked={deleteFiles}
          disabled={busy}
          onChange={(e) => setDeleteFiles(e.target.checked)}
          label={t("Also delete mirrored data and viewer files")}
          description={t(
            "This frees local space and cannot be undone. Original rollout files are kept.",
          )}
        />
        {error && (
          <p className="pg-live-bad" role="alert">
            {error}
          </p>
        )}
      </Dialog>
      <ConfirmDialog
        open={!!plan}
        title={`${t(label)}: ${title}`}
        tone="danger"
        busy={busy}
        confirmLabel={t("Delete")}
        onConfirm={() => void apply()}
        onCancel={() => {
          if (!locked.current) setPlan(null);
        }}
        description={
          <>
            {t(consequence)}
            <br />
            {plan && (
              <>
                {t("Deletion preview: {files} file(s), {size} MiB")
                  .replace("{files}", String(plan.files))
                  .replace("{size}", (plan.bytes / 1048576).toFixed(2))}
                {plan.paths.length > 0 && (
                  <span className="pg-live-deletion-paths">
                    {plan.paths.map((path) => (
                      <code key={path}>{path}</code>
                    ))}
                  </span>
                )}
              </>
            )}
          </>
        }
      />
    </>
  );
}

export function ViewerAction({
  url,
  status,
  fallback,
  dataset,
  updatedAt,
  onChanged,
  request = leviRequest,
}: {
  url?: string | null;
  status?: string | null;
  fallback?: LiveLink | null;
  dataset?: string;
  updatedAt?: number | null;
  onChanged?: () => void;
  request?: ManagementRequest;
}) {
  const { t } = useLocale();
  const [busy, setBusy] = useState(false);
  const [requested, setRequested] = useState(false);
  const [error, setError] = useState("");
  const locked = useRef(false);
  useEffect(() => {
    setRequested(false);
    setError("");
  }, [status, url, dataset, updatedAt]);
  const pending = status === "pending" || status === "error";
  const preparing =
    requested || ["preparing", "building", "refreshing"].includes(status ?? "");
  const link = url ? { href: url, external: !url.startsWith("/") } : fallback;
  async function prepare() {
    if (!dataset || locked.current || requested) return;
    locked.current = true;
    setBusy(true);
    setError("");
    try {
      const result = await request<{ running?: boolean }>(
        "POST",
        `live/datasets/${encodeURIComponent(dataset)}/prepare`,
        {},
      );
      // A worker can finish before this response, or the prepare may do
      // nothing. Only a confirmed running process warrants a local wait.
      // A later catalogue timestamp clears that wait even if it failed and
      // the server's status remained "error" throughout the retry.
      setRequested(result.running === true);
      onChanged?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      locked.current = false;
      setBusy(false);
    }
  }
  if (link && !preparing && !pending && status !== "error") {
    const content = (
      <>
        {t("Open in the viewer")}
        <Icon icon={ArrowUpRight} />
      </>
    );
    const className = "ds-btn ds-btn--secondary ds-btn--sm ds-focus";
    return link.external ? (
      <a
        className={className}
        href={link.href}
        target="_blank"
        rel="noopener noreferrer"
      >
        {content}
      </a>
    ) : (
      <Link className={className} href={link.href}>
        {content}
      </Link>
    );
  }
  return (
    <span className="pg-live-view-unavailable">
      <Button size="sm" disabled>
        {t("Open in the viewer")}
      </Button>
      {pending && dataset && !preparing && (
        <Button
          size="sm"
          variant="ghost"
          loading={busy}
          onClick={() => void prepare()}
        >
          {t("Prepare dataset viewer")}
        </Button>
      )}
      <span className="pg-live-action-note">
        {preparing
          ? t("The dataset viewer is being prepared.")
          : status === "error"
            ? t("The dataset viewer could not be prepared.")
            : t("The dataset viewer is not ready yet.")}
      </span>
      {error && (
        <span className="pg-live-action-note pg-live-bad" role="alert">
          {error}
        </span>
      )}
    </span>
  );
}
