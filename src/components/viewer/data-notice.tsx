"use client";
import { AlertTriangle } from "lucide-react";
import { Icon } from "@/components/ds";
import { T, useLocale } from "@/components/levi-locale";
import "./viewer.css";

/**
 * Said in words when data could not be read within the memory limit
 * (ParquetTooLargeError), instead of leaving the charts silently empty. The
 * technical message (sizes and the limit) sits under "Technical details".
 */
export function DataLoadNotice({
  message,
  skippedEpisodes,
}: {
  message: string;
  /** Set for the cross-episode analysis: how many sampled episodes it lacks. */
  skippedEpisodes?: number;
}) {
  const { t } = useLocale();
  return (
    <div className="vw-note vw-note--warning" role="alert">
      <div className="vw-note-head">
        <Icon icon={AlertTriangle} />
        <strong>
          {skippedEpisodes === undefined ? (
            <T>{"This episode's data is too large to chart"}</T>
          ) : (
            <T>Some episodes were left out of this analysis</T>
          )}
        </strong>
      </div>
      <p>
        {skippedEpisodes === undefined ? (
          <T>
            The data file behind this episode is larger than LEVI reads into
            memory at once, so the charts are empty. The videos and annotations
            still work.
          </T>
        ) : (
          <>
            <T>Episodes left out because their data file is too large:</T>{" "}
            {skippedEpisodes}
          </>
        )}
      </p>
      <details>
        <summary>{t("Technical details")}</summary>
        <pre className="m-0 whitespace-pre-wrap">{message}</pre>
      </details>
    </div>
  );
}
