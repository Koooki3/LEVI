"use client";
import Link from "next/link";
import { AlertTriangle, RotateCw } from "lucide-react";
import { Button, Icon } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { useTitleOverride } from "@/components/shell/route-title";
import "./viewer.css";

/**
 * The episode viewer's error, in three parts: what happened, why (the
 * original message, under "Technical details"), what to do (try again, or
 * go back to Explore).
 */
export function EpisodeLoadError({
  message,
  onRetry,
}: {
  message: string;
  onRetry: () => void;
}) {
  const { t } = useLocale();
  // The address (episode_abc, episode_-1) may be no episode at all: name the
  // tab for what the page says.
  useTitleOverride("This episode could not be loaded");
  return (
    <main className="vw-error ds-root">
      <div className="vw-error-card" role="alert">
        <h2>
          <Icon icon={AlertTriangle} size="md" />
          {t("This episode could not be loaded")}
        </h2>
        <p className="m-0 vw-muted">
          {t(
            "LEVI could not read the dataset files for this episode. The dataset may have moved, a file may be missing, or a remote dataset may need you to sign in to Hugging Face.",
          )}
        </p>
        <details>
          <summary>{t("Technical details")}</summary>
          <pre>{t(message)}</pre>
        </details>
        <div className="flex flex-wrap gap-2">
          <Button variant="primary" icon={RotateCw} onClick={onRetry}>
            {t("Try again")}
          </Button>
          <Link href="/explore" className="ds-btn ds-btn--secondary ds-focus">
            {t("Back to Explore")}
          </Link>
        </div>
      </div>
    </main>
  );
}
