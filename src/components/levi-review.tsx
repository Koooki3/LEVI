"use client";
import { useEffect, useState } from "react";
import { useFlaggedEpisodes } from "@/context/flagged-episodes-context";
import { useLocale } from "./levi-locale";
import { Download, Pencil } from "lucide-react";
import {
  Button,
  Dialog,
  Field,
  IconButton,
  Textarea,
  useToast,
} from "@/components/ds";
import { leviApi, downloadJson, exportName } from "./levi-api";
export default function LeviReview({ repoId }: { repoId: string }) {
  const { flagged } = useFlaggedEpisodes();
  const [busy, setBusy] = useState(false),
    [notes, setNotes] = useState(""),
    [editing, setEditing] = useState(false);
  const { t } = useLocale();
  const toast = useToast();
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
        exportName(repoId, "review"),
      );
      toast.show({ title: t("Review saved"), tone: "success" });
      setEditing(false);
    } catch (e) {
      toast.show({
        title: t("The review was not saved"),
        description: t(String(e).replace(/^(?:[A-Z]\w*)?Error:\s*/, "")),
        tone: "danger",
      });
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="flex items-center gap-1 whitespace-nowrap">
      <Button size="sm" icon={Download} loading={busy} onClick={save}>
        {t("Export review")} ({flagged.size})
      </Button>
      <IconButton
        icon={Pencil}
        size="sm"
        label={t("Review notes")}
        tooltipPlacement="bottom"
        onClick={() => setEditing(true)}
      />
      <Dialog
        open={editing}
        onClose={() => setEditing(false)}
        title={t("Review notes")}
        footer={
          <>
            <Button onClick={() => setEditing(false)}>{t("Cancel")}</Button>
            <Button
              variant="primary"
              icon={Download}
              loading={busy}
              onClick={save}
            >
              {t("Export review")}
            </Button>
          </>
        }
      >
        <Field label={t("Review notes")}>
          <Textarea
            autoFocus
            rows={6}
            value={notes}
            maxLength={20000}
            placeholder={t("Optional review notes")}
            onChange={(e) => setNotes(e.target.value)}
          />
        </Field>
      </Dialog>
    </div>
  );
}
