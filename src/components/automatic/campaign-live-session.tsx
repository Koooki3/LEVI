"use client";
import Link from "next/link";
import { useLocale } from "@/components/levi-locale";
import { Note } from "@/components/pages-ui/feedback";
import { sessionHref } from "@/components/live/session-route";
import type { CampaignLiveSession } from "./wizard-types";
const LABELS: Record<CampaignLiveSession["status"], string> = {
  waiting: "Waiting for this segment's evaluation client to report.",
  linked: "The evaluation client is reporting this segment.",
  ended: "This segment's evaluation session has ended or stopped reporting.",
  ambiguous: "Several sessions match this segment. No session was selected.",
  mismatch: "The evaluation client does not match this segment's settings.",
  unavailable: "The live evaluation status is unavailable.",
};
export function CampaignSessionLink({
  value,
}: {
  value?: CampaignLiveSession;
}) {
  const { t } = useLocale();
  if (!value) return null; // Older servers provide no association.
  const href =
    (value.status === "linked" || value.status === "ended") && value.session
      ? sessionHref(value.session)
      : null;
  return (
    <Note
      tone={
        value.status === "mismatch" || value.status === "ambiguous"
          ? "warning"
          : "info"
      }
    >
      <p>{t(LABELS[value.status])}</p>
      {href && (
        <Link className="ds-btn ds-btn--ghost ds-btn--sm ds-focus" href={href}>
          {t("Session details")}
        </Link>
      )}
    </Note>
  );
}
