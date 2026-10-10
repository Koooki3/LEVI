"use client";

import { useEffect, useId, useMemo, useRef, useState } from "react";
import { Trash2 } from "lucide-react";
import { Button, Checkbox, ConfirmDialog } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { leviRequest } from "@/components/levi-api";

export interface EpisodeDeletionPlan {
  confirmation: string;
  episodes: number[];
  files: number;
  bytes: number;
  paths: string[];
  shared_files_retained: number;
}

export interface EpisodeDeletionResult {
  deleted: boolean | number[];
  remaining: number;
  first_episode_index: number | null;
  repo_id: string;
  cleanup_pending?: boolean;
}

export type EpisodeManagementRequest = <T>(
  method: "POST" | "DELETE",
  path: string,
  body: unknown,
) => Promise<T>;

function indices(values: number[]): number[] {
  return [...new Set(values)]
    .filter((id) => Number.isInteger(id) && id >= 0)
    .sort((a, b) => a - b);
}

/** Full navigation flushes metadata caches after episode files are changed. */
export function episodeDeletionDestination(
  result: EpisodeDeletionResult,
  session?: string | null,
  sessionEmpty = false,
): string {
  if (result.remaining <= 0) return "/workbench";
  const [org, name, extra] = result.repo_id.split("/");
  if (org !== "local" || !name || extra !== undefined) return "/workbench";
  const root = `/local/${encodeURIComponent(name)}`;
  const first = result.first_episode_index;
  const path =
    !session &&
    !sessionEmpty &&
    first !== null &&
    Number.isInteger(first) &&
    first >= 0
      ? `${root}/episode_${first}`
      : root;
  return session
    ? `${path}?${new URLSearchParams({ live_session: session })}`
    : path;
}

type Preview = {
  plan: EpisodeDeletionPlan;
  name: string;
  scope: string;
  session: string | null;
  sessionEmpty: boolean;
  generation: number;
};

export function EpisodeManagement({
  name,
  episodeIds,
  currentEpisode,
  session = null,
  request = leviRequest,
  navigate = (url) => window.location.assign(url),
}: {
  name: string;
  episodeIds: number[];
  currentEpisode: number;
  session?: string | null;
  request?: EpisodeManagementRequest;
  navigate?: (url: string) => void;
}) {
  const { t } = useLocale();
  const listId = useId();
  const visible = useMemo(() => indices(episodeIds), [episodeIds]);
  const recordScope = JSON.stringify([name, session]);
  const scope = JSON.stringify([name, session, visible]);
  const version = useRef({ scope, generation: 0 });
  if (version.current.scope !== scope)
    version.current = { scope, generation: version.current.generation + 1 };
  const generation = version.current.generation;
  const latestScope = useRef(scope);
  latestScope.current = scope;
  const [editing, setEditing] = useState<string | null>(null);
  const [selection, setSelection] = useState<{
    scope: string;
    ids: number[];
  } | null>(null);
  const selected =
    selection?.scope === recordScope
      ? visible.filter((id) => selection.ids.includes(id))
      : visible.filter((id) => id === currentEpisode);
  const selectedSet = new Set(selected);
  const all = visible.length > 0 && selected.length === visible.length;
  const [preview, setPreview] = useState<Preview | null>(null);
  const [busy, setBusy] = useState(false);
  const locked = useRef(false);
  const [error, setError] = useState("");
  const [cleanupNotice, setCleanupNotice] = useState<{
    scope: string;
    destination: string;
  } | null>(null);
  const cleanup = cleanupNotice?.scope === recordScope ? cleanupNotice : null;
  const open = editing === recordScope;
  const confirmation =
    preview?.scope === scope && preview.generation === generation
      ? preview
      : null;
  useEffect(() => {
    setPreview(null);
  }, [scope]);

  function choose(ids: number[]) {
    setSelection({ scope: recordScope, ids });
    setError("");
    setPreview(null);
  }

  async function previewDeletion() {
    if (locked.current || selected.length === 0 || cleanup) return;
    locked.current = true;
    setBusy(true);
    setError("");
    const requested = [...selected];
    const requestedScope = scope;
    const requestedGeneration = generation;
    try {
      const plan = await request<EpisodeDeletionPlan>(
        "POST",
        `datasets/${encodeURIComponent(name)}/episodes/deletion-plan`,
        { episodes: requested },
      );
      if (
        latestScope.current !== requestedScope ||
        version.current.generation !== requestedGeneration
      ) {
        setError(
          t(
            "Episode list changed. Review the selection and preview deletion again.",
          ),
        );
        return;
      }
      if (
        !Array.isArray(plan.episodes) ||
        JSON.stringify(indices(plan.episodes)) !== JSON.stringify(requested)
      ) {
        throw new Error(
          t("Deletion preview does not match the selected episodes."),
        );
      }
      setPreview({
        plan,
        name,
        scope: requestedScope,
        session,
        sessionEmpty: !!session && requested.length === visible.length,
        generation: requestedGeneration,
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      locked.current = false;
      setBusy(false);
    }
  }

  async function deleteEpisodes() {
    if (!confirmation || locked.current) return;
    locked.current = true;
    setBusy(true);
    setError("");
    try {
      const result = await request<EpisodeDeletionResult>(
        "DELETE",
        `datasets/${encodeURIComponent(confirmation.name)}/episodes`,
        {
          episodes: confirmation.plan.episodes,
          confirmation: confirmation.plan.confirmation,
        },
      );
      setPreview(null);
      const destination = episodeDeletionDestination(
        result,
        confirmation.session,
        confirmation.sessionEmpty,
      );
      if (result.cleanup_pending) {
        setEditing(null);
        setCleanupNotice({
          scope: JSON.stringify([confirmation.name, confirmation.session]),
          destination,
        });
      } else navigate(destination);
    } catch (e) {
      // A changed or busy dataset needs a new preview, never a retry using
      // an earlier confirmation fingerprint.
      setPreview(null);
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      locked.current = false;
      setBusy(false);
    }
  }

  return (
    <section
      className="vw-episode-management"
      aria-label={t("Episode file management")}
    >
      <Button
        variant="ghost"
        size="sm"
        icon={Trash2}
        disabled={visible.length === 0 || busy || !!cleanup}
        aria-expanded={open}
        aria-controls={listId}
        onKeyDown={(event) => event.stopPropagation()}
        onClick={() => {
          setEditing(open ? null : recordScope);
          setError("");
          setPreview(null);
        }}
      >
        {t("Manage episode files")}
      </Button>
      {visible.length === 0 && (
        <p className="vw-episode-management-note">
          {t("No visible episodes to delete.")}
        </p>
      )}
      <div id={listId} hidden={!open}>
        {open && (
          <>
            <Checkbox
              label={t("Select all visible episodes")}
              checked={all}
              indeterminate={selected.length > 0 && !all}
              disabled={busy}
              onKeyDown={(event) => event.stopPropagation()}
              onChange={() => choose(all ? [] : visible)}
            />
            <div
              className="vw-episode-management-list"
              role="group"
              aria-label={t("Select episodes to delete")}
            >
              {visible.map((id) => (
                <Checkbox
                  key={id}
                  label={t(`Episode ${id}`)}
                  description={
                    id === currentEpisode ? t("Current episode") : undefined
                  }
                  checked={selectedSet.has(id)}
                  disabled={busy}
                  onKeyDown={(event) => event.stopPropagation()}
                  onChange={(e) =>
                    choose(
                      e.target.checked
                        ? [...selected, id]
                        : selected.filter((other) => other !== id),
                    )
                  }
                />
              ))}
            </div>
            <p className="vw-episode-management-note">
              {t("Selected: {count}").replace(
                "{count}",
                String(selected.length),
              )}
            </p>
            <div className="vw-episode-management-actions">
              <Button
                size="sm"
                disabled={busy}
                onKeyDown={(event) => event.stopPropagation()}
                onClick={() => {
                  setEditing(null);
                  setPreview(null);
                  setError("");
                }}
              >
                {t("Cancel")}
              </Button>
              <Button
                size="sm"
                variant="danger"
                disabled={selected.length === 0 || !!cleanup}
                loading={busy}
                onKeyDown={(event) => event.stopPropagation()}
                onClick={() => void previewDeletion()}
              >
                {t("Preview deletion")}
              </Button>
            </div>
            {selected.length === 0 && (
              <p className="vw-episode-management-note">
                {t("Select at least one episode.")}
              </p>
            )}
          </>
        )}
      </div>
      {busy && (
        <p className="vw-episode-management-note" role="status">
          {t("Working…")}
        </p>
      )}
      {error && (
        <p className="vw-episode-management-error" role="alert">
          {error}
        </p>
      )}
      {cleanup && (
        <div className="vw-episode-management-note" role="status">
          <p>
            {t(
              "Dataset metadata was updated, but some staged files still need cleanup.",
            )}
          </p>
          <Button
            size="sm"
            onKeyDown={(event) => event.stopPropagation()}
            onClick={() => navigate(cleanup.destination)}
          >
            {t("Reload remaining episodes")}
          </Button>
        </div>
      )}
      <ConfirmDialog
        open={!!confirmation}
        title={t("Delete {count} episode(s) and local files?").replace(
          "{count}",
          String(confirmation?.plan.episodes.length ?? 0),
        )}
        confirmLabel={t("Delete episodes and local files")}
        tone="danger"
        busy={busy}
        onConfirm={() => void deleteEpisodes()}
        onCancel={() => {
          if (!locked.current) setPreview(null);
        }}
        description={
          <>
            {t(
              "Local file deletion permanently removes the selected episodes, including original rollout data, local mirrors and files used only by those episodes. This cannot be undone.",
            )}
            {confirmation && (
              <>
                <br />
                {t("Deletion preview: {files} file(s), {size} MiB")
                  .replace("{files}", String(confirmation.plan.files))
                  .replace(
                    "{size}",
                    (confirmation.plan.bytes / 1048576).toFixed(2),
                  )}
                {confirmation.plan.shared_files_retained > 0 && (
                  <>
                    <br />
                    {t(
                      "Shared data files used by other episodes will be kept: {count}.",
                    ).replace(
                      "{count}",
                      String(confirmation.plan.shared_files_retained),
                    )}
                  </>
                )}
                {confirmation.plan.paths.length > 0 && (
                  <span className="vw-episode-management-paths">
                    {confirmation.plan.paths.map((path) => (
                      <code key={path}>{path}</code>
                    ))}
                  </span>
                )}
              </>
            )}
          </>
        }
      />
    </section>
  );
}
