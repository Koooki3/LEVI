"use client";
import { ExpandableText } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { canShowResult } from "./result-api";
import { count, percent } from "./stats-logic";
import type { ResultItem } from "./result-types";
export function PolicyComparison({ item }: { item: ResultItem }) {
  const { t } = useLocale();
  const comparison = item.comparison;
  const conditions = canShowResult(item) ? comparison.conditions : null;
  return (
    <details className="pg-live-disclosure">
      <summary>{t("Policy comparison conditions")}</summary>
      <p>
        {t(
          comparison.status === "comparable"
            ? "Explicit campaign comparison available"
            : comparison.status === "incompatible"
              ? "Comparison conditions differ"
              : "Comparison association is unknown",
        )}
      </p>
      {comparison.campaign_id && (
        <p>
          {t("Campaign")}: <code>{comparison.campaign_id}</code>
        </p>
      )}
      <p className="pg-pool-hint">
        {t(
          "Policies are not ranked here. Only an explicit campaign with matching frozen conditions can support a comparison.",
        )}
      </p>
      {comparison.missing.length > 0 && (
        <>
          <p>{t("Missing comparison conditions")}</p>
          <ul>
            {comparison.missing.map((value) => (
              <li key={value}>
                <code>{t(value)}</code>
              </li>
            ))}
          </ul>
        </>
      )}
      {!!comparison.differences?.length && (
        <>
          <p>{t("Different comparison conditions")}</p>
          <ul>
            {comparison.differences.map((value) => (
              <li key={value}>
                <code>{t(value)}</code>
              </li>
            ))}
          </ul>
        </>
      )}
      {conditions && (
        <>
          <dl className="pg-live-dl">
            <dt>{t("Reset mode")}</dt>
            <dd>{conditions.reset_mode ? t(conditions.reset_mode) : "—"}</dd>
            <dt>{t("Label basis")}</dt>
            <dd>{t(conditions.label_basis)}</dd>
            <dt>{t("Consecutive fault stop rule")}</dt>
            <dd>{count(conditions.stop_rules?.consecutive_faults)}</dd>
            <dt>{t("Unplanned intervention stop rule")}</dt>
            <dd>
              {count(conditions.stop_rules?.unplanned_interventions_per_arm)}
            </dd>
          </dl>
          <details>
            <summary>{t("Policies and environment versions")}</summary>
            <div className="pg-live-result-records">
              {conditions.arms.map((arm, index) => (
                <article
                  key={`${arm.id}/${index}`}
                  className="pg-live-result-record"
                >
                  <h3>{arm.id || "—"}</h3>
                  <dl className="pg-live-dl">
                    <dt>{t("Policy configuration")}</dt>
                    <dd>
                      <ExpandableText
                        text={arm.config || "—"}
                        mono
                        previewLength={48}
                      />
                    </dd>
                    <dt>{t("Policy checkpoint")}</dt>
                    <dd>
                      <ExpandableText
                        text={arm.checkpoint || "—"}
                        mono
                        previewLength={48}
                      />
                    </dd>
                    <dt>{t("Environment versions")}</dt>
                    <dd>
                      <ExpandableText
                        text={arm.versions || "—"}
                        mono
                        previewLength={48}
                      />
                    </dd>
                  </dl>
                  {!!arm.not_verified?.length && (
                    <>
                      <p>{t("Unverified conditions")}</p>
                      <ul>
                        {arm.not_verified.map((value) => (
                          <li key={value}>{t(value)}</li>
                        ))}
                      </ul>
                    </>
                  )}
                </article>
              ))}
            </div>
          </details>
          <details>
            <summary>{t("Campaign run conditions")}</summary>
            <div className="pg-live-result-records">
              {conditions.runs.map((run, index) => (
                <article
                  key={`${run.run_id}/${index}`}
                  className="pg-live-result-record"
                >
                  <h3>
                    <code>{run.run_id || "—"}</code>
                  </h3>
                  <dl className="pg-live-dl">
                    <dt>{t("Reset mode")}</dt>
                    <dd>{run.reset_mode ? t(run.reset_mode) : "—"}</dd>
                    <dt>{t("Scene check")}</dt>
                    <dd>{run.scene_check ? t(run.scene_check) : "—"}</dd>
                    <dt>{t("Running commit")}</dt>
                    <dd>
                      <code>{run.levi_commit || "—"}</code>
                    </dd>
                    <dt>{t("Frozen plan")}</dt>
                    <dd>
                      <code>{run.plan_sha256 || "—"}</code>
                    </dd>
                  </dl>
                </article>
              ))}
            </div>
          </details>
          <details>
            <summary>{t("Sample and early stop coverage")}</summary>
            {conditions.coverage && (
              <ul>
                {Object.entries(conditions.coverage).map(([arm, value]) => (
                  <li key={arm}>
                    <code>{arm}</code> · {t("Label coverage")}:{" "}
                    {count(value.labelled)} / {count(value.valid)} ·{" "}
                    {percent(value.coverage)} · {t("Unlabelled")}:{" "}
                    {count(value.unlabelled)}
                  </li>
                ))}
              </ul>
            )}
            {conditions.early_stop?.truth_basis && (
              <p>
                {t("Early stop label basis")}:{" "}
                {t(conditions.early_stop.truth_basis)}
              </p>
            )}
            {conditions.early_stop?.arms && (
              <ul>
                {Object.entries(conditions.early_stop.arms).map(
                  ([arm, value]) => (
                    <li key={arm}>
                      <code>{arm}</code> · {t("Episodes")}:{" "}
                      {count(value.episodes)} · {t("Control episodes")}:{" "}
                      {count(value.controls)} · {t("False early stop rate")}:{" "}
                      {percent(value.false_early_stop_rate?.rate)} (
                      {count(value.false_early_stop_rate?.k)} /{" "}
                      {count(value.false_early_stop_rate?.n)})
                    </li>
                  ),
                )}
              </ul>
            )}
          </details>
        </>
      )}
    </details>
  );
}
