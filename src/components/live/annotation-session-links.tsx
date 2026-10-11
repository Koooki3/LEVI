"use client";
import Link from "next/link";
import { useLocale } from "@/components/levi-locale";
import { linkedAnnotation, resultIndexPath } from "./result-api";
import { resultIdentity } from "./result-types";
import { identityHref } from "./session-route";
import { useResults } from "./use-results";
// An annotation run is not an evaluation run: only a verified index join creates a backlink.
export function AnnotationSessionLinks({
  repo,
  run,
}: {
  repo: string;
  run: string;
}) {
  const { t } = useLocale();
  const dataset = repo.startsWith("local/") ? repo.slice(6) : "";
  const request = useResults(
    dataset && run ? resultIndexPath(dataset, run) : null,
  );
  if (!dataset) return null;
  const index = request.data && "items" in request.data ? request.data : null;
  const items =
    index?.items.filter((item) => linkedAnnotation(item, repo, run)) ?? [];
  return (
    <div className="pg-live-results">
      <p className="pg-pool-hint">
        {t("Evaluation sessions linked to this annotation run")}
      </p>
      {request.error ? (
        <p>
          {t("Could not read result sources")}: {request.error}
        </p>
      ) : !index ? (
        <p>{t("Loading…")}</p>
      ) : items.length === 0 ? (
        <p>
          {t(
            "No verified evaluation session is linked to this annotation run.",
          )}
        </p>
      ) : (
        <ul>
          {items.map((item) => (
            <li key={JSON.stringify(item.identity)}>
              <Link
                className="ds-focus"
                href={`${identityHref(resultIdentity(item))}#session-analysis`}
              >
                {item.identity.group} / {item.identity.task} ·{" "}
                {item.identity.run_id || item.identity.session_id}
              </Link>
              <p>
                <code>{item.identity.root}</code>
              </p>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
