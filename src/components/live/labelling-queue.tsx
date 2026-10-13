"use client";
// Which datasets wait for labelling, which one is being labelled and, when
// labelling steps aside for the GPU, why and what brings it back. It shows the
// service's status rows; it changes nothing.
import { useLocale } from "@/components/levi-locale";
import { clock } from "./live-logic";
import {
  QUEUE_MODE_LABEL,
  QUEUE_STATE_LABEL,
  queueRowReason,
  queueRows,
  resumeText,
  yieldReasonText,
  yieldRecoveryText,
} from "./queue-logic";
import { Chip, type Tone } from "./session-panels";
import type { YieldInfo } from "./types";

const STATE_TONE: Record<string, Tone> = {
  running: "pass",
  done: "pass",
  failed: "fail",
  yielding: "warn",
  paused: "warn",
  queued: "",
};

export function LabellingQueue({
  rows,
  yieldInfo,
}: {
  rows: unknown;
  yieldInfo?: YieldInfo | null;
}) {
  const { t } = useLocale();
  const list = queueRows(rows);
  const yielding = Boolean(yieldInfo?.active);
  if (!list.length && !yielding) return null;
  const resume = resumeText(yieldInfo, t);
  return (
    <div className="pg-live-queue" data-testid="labelling-queue">
      {yielding && (
        <div className="pg-live-gate warn" role="status">
          <strong>{t("Labelling is yielding the GPU")}</strong>
          {typeof yieldInfo?.since === "number" && (
            <span className="pg-pool-muted">
              {" "}
              {t("since")} {clock(yieldInfo.since)}
            </span>
          )}
          <ul>
            {(yieldInfo?.reasons ?? []).map((reason, index) => {
              const recovery = yieldRecoveryText(reason, t);
              return (
                <li key={`${reason.code}-${index}`}>
                  {yieldReasonText(reason, t)}
                  {recovery && (
                    <span className="pg-pool-muted"> {recovery}</span>
                  )}
                </li>
              );
            })}
          </ul>
          {resume && <p>{resume}</p>}
        </div>
      )}
      {list.length > 0 && (
        <table className="pg-live-queue-table">
          <caption className="pg-pool-muted">
            {t("Datasets in the labelling queue")}
          </caption>
          <thead>
            <tr>
              <th scope="col">{t("Dataset")}</th>
              <th scope="col">{t("State")}</th>
              <th scope="col">{t("Mode")}</th>
              <th scope="col">{t("Waiting")}</th>
              <th scope="col">{t("Reason")}</th>
            </tr>
          </thead>
          <tbody>
            {list.map((row) => {
              const state = row.state ?? "";
              return (
                <tr key={row.dataset} data-state={state}>
                  <td>
                    <code>{row.dataset}</code>
                  </td>
                  <td>
                    <Chip tone={STATE_TONE[state] ?? ""}>
                      {QUEUE_STATE_LABEL[state]
                        ? t(QUEUE_STATE_LABEL[state])
                        : state || "—"}
                    </Chip>
                  </td>
                  <td>
                    {row.mode && QUEUE_MODE_LABEL[row.mode]
                      ? t(QUEUE_MODE_LABEL[row.mode])
                      : (row.mode ?? "—")}
                  </td>
                  <td>{row.waiting ?? 0}</td>
                  <td>{queueRowReason(row, yieldInfo, t)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}
