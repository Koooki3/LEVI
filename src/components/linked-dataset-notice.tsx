"use client";
import { Lock } from "lucide-react";
import { Badge, Icon } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { useDatasetSource } from "@/context/dataset-source-context";
import {
  LINKED_NOTICE,
  LINKED_READ_ONLY,
  LINKED_REASON,
} from "@/utils/linkedDataset";
import "@/components/viewer/viewer.css";

/** The one fact a person needs on a live evaluation workspace's dataset,
 * shown above everything the viewer offers: it is read-only here, and where
 * to label or review it. Nothing for any other dataset. */
export function LinkedDatasetNotice() {
  const { linked } = useDatasetSource();
  const { t } = useLocale();
  if (!linked) return null;
  return (
    <div className="vw-note vw-note--linked" role="note">
      <div className="vw-note-head">
        <Icon icon={Lock} />
        <Badge tone="info" icon={null}>
          {t("Live evaluation · read-only")}
        </Badge>
      </div>
      <p>{t(LINKED_NOTICE)}</p>
    </div>
  );
}

/** Why a control is off on a linked dataset, in words next to it (a tooltip
 * alone never carries a reason). `id` is what the control's
 * `aria-describedby` points at. Nothing for any other dataset. */
export function ReadOnlyReason({
  id,
  full = false,
}: {
  id?: string;
  /** The service's own sentence (what a refused write says), not the short
   * reason. */
  full?: boolean;
}) {
  const { linked } = useDatasetSource();
  const { t } = useLocale();
  if (!linked) return null;
  return (
    <span id={id} className="vw-muted vw-readonly-reason">
      {t(full ? LINKED_READ_ONLY : LINKED_REASON)}
    </span>
  );
}
