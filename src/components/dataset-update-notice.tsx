"use client";
import Link from "next/link";
import { T, useLocale } from "@/components/levi-locale";
import { useDatasetSource } from "@/context/dataset-source-context";

/** Floating notice when the open dataset changed on disk (levi/sync.py):
 * episodes added/removed, a raw capture's view rebuilt, or the dataset
 * removed. Reloading is the user's choice — unsaved annotation edits are
 * never discarded behind their back. */
export function DatasetUpdateNotice() {
  const { t } = useLocale();
  const { changed, removed, entry, format, loadedEpisodes } =
    useDatasetSource();
  const rebuilding =
    format?.kind === "raw" && entry?.view_status === "building";
  if (!changed && !removed && !rebuilding) return null;
  const now = format?.episodes ?? null;
  return (
    <div className="levi-update-toast" role="status" aria-live="polite">
      {removed ? (
        <>
          <strong>
            <T>This dataset was removed from the workspace</T>
          </strong>
          <p>
            <T>
              Its folder is gone. Annotations and review flags are kept and come
              back if the dataset is added again.
            </T>
          </p>
          <Link className="levi-secondary" href="/workbench">
            <T>Open the Workbench</T>
          </Link>
        </>
      ) : changed ? (
        <>
          <strong>
            <T>This dataset changed on disk</T>
          </strong>
          {loadedEpisodes != null && now != null && loadedEpisodes !== now && (
            <p className="tabular">
              {t("Episodes")}: {loadedEpisodes} → {now}
            </p>
          )}
          <p>
            <T>
              Reload to see the current episodes. Save any open annotation edits
              first.
            </T>
          </p>
          <button
            type="button"
            className="levi-primary"
            onClick={() => window.location.reload()}
          >
            <T>Reload</T>
          </button>
        </>
      ) : (
        <>
          <strong>
            <T>The capture changed — updating its browsing view…</T>
          </strong>
          <p>
            <T>You can keep working; a reload is offered when it is ready.</T>
          </p>
        </>
      )}
    </div>
  );
}
