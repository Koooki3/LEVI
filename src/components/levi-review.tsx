"use client";
import { useEffect, useState } from "react";
import { useFlaggedEpisodes } from "@/context/flagged-episodes-context";
import { T, useLocale } from "./levi-locale";
import { leviApi, downloadJson } from "./levi-api";
export default function LeviReview({ repoId }: { repoId: string }) {
  const { flagged } = useFlaggedEpisodes();
  const [busy, setBusy] = useState(false),
    [message, setMessage] = useState(""),
    [notes, setNotes] = useState(""),
    [editing, setEditing] = useState(false);
  const { t } = useLocale();
  useEffect(() => {
    let current = true;
    leviApi<{ notes: string }>(`review?repo_id=${encodeURIComponent(repoId)}`)
      .then((r) => {
        if (current) setNotes(r.notes || "");
      })
      .catch(() => {});
    return () => {
      current = false;
    };
  }, [repoId]);
  async function save() {
    setBusy(true);
    try {
      const review = await leviApi("review", {
        repo_id: repoId,
        flagged: [...flagged],
        notes,
      });
      downloadJson(
        {
          ...(review as object),
          schema: "levi.review.v1",
          excluded_episode_ids: [...flagged],
        },
        "levi-review.json",
      );
      setMessage("Review saved");
      setEditing(false);
    } catch (e) {
      setMessage(String(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="flex items-center gap-2 px-3 text-xs whitespace-nowrap">
      <button className="levi-secondary" disabled={busy} onClick={save}>
        <T>Export review</T> ({flagged.size})
      </button>
      <button title={t("Review notes")} onClick={() => setEditing(true)}>
        ✎
      </button>
      {message && (
        <span role="status">
          <T>{message}</T>
        </span>
      )}
      {editing && (
        <div
          className="fixed inset-0 z-[100] flex items-center justify-center bg-black/70"
          role="dialog"
          aria-modal="true"
          aria-label={t("Review notes")}
        >
          <div className="levi-box w-[min(520px,90vw)]">
            <h2>
              <T>Review notes</T>
            </h2>
            <textarea
              autoFocus
              className="levi-input w-full h-36 mt-4"
              value={notes}
              maxLength={20000}
              aria-label={t("Review notes")}
              placeholder={t("Optional review notes")}
              onChange={(e) => setNotes(e.target.value)}
            />
            <div className="levi-row mt-4">
              <button className="levi-primary" disabled={busy} onClick={save}>
                <T>Export review</T>
              </button>
              <button
                className="levi-secondary"
                onClick={() => setEditing(false)}
              >
                <T>Cancel</T>
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
