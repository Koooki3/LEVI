"use client";
import { useState } from "react";
import Link from "next/link";
import {
  Button,
  ExpandableText,
  Field,
  Input,
  SkeletonText,
} from "@/components/ds";
import { Note, Problem } from "@/components/pages-ui/feedback";
import { useLocale } from "@/components/levi-locale";
import { canShowResult, resultIndexPath } from "./result-api";
import { useResults } from "./use-results";
import { resultIdentity, type ResultItem } from "./result-types";
import { identityHref } from "./session-route";
import { PolicyComparison } from "./policy-comparison";
import { ResultState } from "./session-results";
import "./results.css";
export function resultKey(item: ResultItem): string {
  return JSON.stringify(item.identity);
}
export function matchingResults(
  items: ResultItem[],
  text: string,
): ResultItem[] {
  const terms = text.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  return items.filter((item) => {
    const haystack = [
      item.identity.root,
      item.identity.group,
      item.identity.task,
      item.identity.run_id,
      item.identity.session_id,
      ...item.identity.datasets,
      item.policy?.config,
      item.policy?.checkpoint,
      item.status,
      item.comparison.campaign_id,
      ...(item.source?.catalog_versions ?? []).map(
        (report) => `${report.basis} ${report.version}`,
      ),
      ...(canShowResult(item) ? item.reports : []).map(
        (report) => `${report.basis} ${report.version}`,
      ),
    ]
      .join(" ")
      .toLocaleLowerCase();
    return terms.every((term) => haystack.includes(term));
  });
}
export function ResultDirectory() {
  const { t } = useLocale();
  const request = useResults(resultIndexPath());
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(0);
  const index = request.data && "items" in request.data ? request.data : null;
  const items = matchingResults(index?.items ?? [], search);
  const pages = Math.ceil(items.length / 6);
  const current = Math.min(page, Math.max(0, pages - 1));
  return (
    <section className="pg-live-results" aria-label={t("Result source index")}>
      <p className="pg-pool-hint">
        {t(
          "Search model, task, source root, session, label basis or report version. Identical names from different source roots stay separate.",
        )}
      </p>
      <div className="pg-live-result-filter">
        <Field label={t("Find a result source")}>
          <Input
            value={search}
            onChange={(e) => {
              setSearch(e.target.value);
              setPage(0);
            }}
          />
        </Field>
        <div>
          <Button size="sm" variant="ghost" onClick={request.refresh}>
            {t("Refresh")}
          </Button>
        </div>
      </div>
      {request.error ? (
        <Problem
          title={t("Could not read result sources")}
          why={request.error}
        />
      ) : !index ? (
        <SkeletonText lines={3} />
      ) : !index.available ? (
        <Note tone="info">{t("The result source index is unavailable.")}</Note>
      ) : (
        <>
          {index.complete === false && (
            <Note tone="info">
              {t(
                "The source scan reached its limit. This index is incomplete; absence does not mean no result exists.",
              )}
              <details>
                <summary>{t("Source scan limits")}</summary>
                <ul>
                  {index.truncated?.map((value) => (
                    <li key={value}>{t(value)}</li>
                  ))}
                  {Object.entries(index.limits ?? {}).map(([key, value]) => (
                    <li key={key}>
                      {t(key)}: {value}
                    </li>
                  ))}
                </ul>
              </details>
            </Note>
          )}
          <p className="pg-pool-muted">
            {items.length} / {index.items.length}
          </p>
          {items.length === 0 && (
            <Note tone="info">{t("No result sources match this search.")}</Note>
          )}
          <div className="pg-live-result-records">
            {items.slice(current * 6, current * 6 + 6).map((item) => (
              <article key={resultKey(item)} className="pg-live-result-record">
                <h3>
                  <Link
                    className="ds-focus"
                    href={`${identityHref(resultIdentity(item))}#session-analysis`}
                  >
                    {item.identity.group} / {item.identity.task} ·{" "}
                    {item.identity.run_id || item.identity.session_id}
                  </Link>
                </h3>
                <ResultState item={item} />
                <dl className="pg-live-dl">
                  <dt>{t("Source root")}</dt>
                  <dd>
                    <ExpandableText
                      text={item.identity.root}
                      mono
                      previewLength={48}
                    />
                  </dd>
                  <dt>{t("Policy checkpoint")}</dt>
                  <dd>
                    <ExpandableText
                      text={item.policy?.checkpoint || "—"}
                      mono
                      previewLength={48}
                    />
                  </dd>
                  <dt>{t("Version")}</dt>
                  <dd>
                    {canShowResult(item) && item.reports.length
                      ? item.reports
                          .map((report) => report.version || "—")
                          .join(" · ")
                      : "—"}
                  </dd>
                  <dt>{t("Label basis")}</dt>
                  <dd>
                    {canShowResult(item) && item.reports.length
                      ? item.reports.map((report) => report.basis).join(" · ")
                      : "—"}
                  </dd>
                </dl>
                {item.source?.catalog_versions?.length ? (
                  <details>
                    <summary>{t("Unverified source versions")}</summary>
                    <ul>
                      {item.source.catalog_versions.map((version, index) => (
                        <li key={index}>
                          {version.basis} · {version.version || "—"} ·{" "}
                          {t("Unverified")}
                        </li>
                      ))}
                    </ul>
                  </details>
                ) : null}
                <PolicyComparison item={item} />
              </article>
            ))}
          </div>
          {pages > 1 && (
            <nav
              className="pg-live-pagination"
              aria-label={t("Result source pages")}
            >
              <Button
                size="sm"
                disabled={current === 0}
                onClick={() => setPage(current - 1)}
              >
                {t("Previous")}
              </Button>
              <span aria-live="polite">
                {t("Page {page} of {pages}")
                  .replace("{page}", String(current + 1))
                  .replace("{pages}", String(pages))}
              </span>
              <Button
                size="sm"
                disabled={current + 1 >= pages}
                onClick={() => setPage(current + 1)}
              >
                {t("Next")}
              </Button>
            </nav>
          )}
        </>
      )}
    </section>
  );
}
export function ResultSourcesDisclosure() {
  const { t } = useLocale();
  const [open, setOpen] = useState(false);
  return (
    <details
      className="pg-live-section pg-live-disclosure"
      id="live-results-region"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>{t("Result sources and policy comparison")}</summary>
      {open && <ResultDirectory />}
    </details>
  );
}
