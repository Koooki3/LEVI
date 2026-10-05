// Modified for LEVI (2026); see NOTICE and docs/UPSTREAM.md.
"use client";
import { LoaderCircle } from "lucide-react";
import { Icon } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import "@/components/viewer/viewer.css";

export default function Loading() {
  const { t } = useLocale();
  // A loading overlay is a status, not a dialog: it takes no focus and traps
  // nothing; screen readers hear "Loading" once (polite). It fades in only
  // after 300 ms, so a fast load never flashes it (proposal §7.2).
  return (
    <div
      className="vw-loading"
      role="status"
      aria-live="polite"
      aria-busy="true"
    >
      <Icon icon={LoaderCircle} size="lg" className="ds-spin" />
      <p className="vw-loading-title">{t("Loading")}</p>
      <p className="vw-loading-detail">{t("preparing data & videos")}</p>
    </div>
  );
}
