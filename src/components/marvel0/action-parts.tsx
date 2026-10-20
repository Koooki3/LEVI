"use client";
// Small pieces the two action cards share: server text in the page's words
// (or as written and marked English), a read that failed, a refusal, and the
// notice that the site file is missing or invalid. Nothing here sends a
// request or starts anything.
import type { ReactNode } from "react";
import { useLocale } from "@/components/levi-locale";
import { Problem } from "@/components/pages-ui/feedback";
import type { ApiFailure } from "./deploy-api";
import {
  failureDetail,
  sayFailure,
  type FailureContext,
} from "./marvel0-failure";
import { list, rec, str, type Worded } from "./marvel0-json";
import type { Say } from "./marvel0-logic";
import { sayText } from "./marvel0-say";
import { isFormat, type ReadProblem } from "./use-marvel0-actions";

/** A piece of server text: in the page's words when the page knows it, else as
 * the server wrote it, which is English and marked so. */
export function WordedText({ worded }: { worded: Worded<Say> }): ReactNode {
  const { t } = useLocale();
  return worded.say ? (
    sayText(worded.say, t)
  ) : (
    <span lang="en">{worded.text}</span>
  );
}

/** The server's own words that go with a failure (what `systemd-run` said, a
 * proxy's page), as written. */
export function FailureDetail({ failure }: { failure: ApiFailure }) {
  const detail = failureDetail(failure);
  return detail ? (
    <code lang="en" className="rg-server">
      {detail}
    </code>
  ) : null;
}

/** A refusal or failed call of a write, with what to do about it. */
export function Refusal({
  testId,
  title,
  failure,
  context,
  worded,
}: {
  testId: string;
  /** Catalogue key of what did not happen. */
  title: string;
  failure: ApiFailure;
  context?: FailureContext;
  /** The words for the failure when the card words it differently. */
  worded?: Worded<Say>;
}) {
  const { t } = useLocale();
  return (
    <div data-testid={testId}>
      <Problem
        live
        title={t(title)}
        why={
          <>
            <WordedText worded={worded ?? sayFailure(failure, context)} />
            <FailureDetail failure={failure} />
          </>
        }
      />
    </div>
  );
}

/** A read that did not give a usable answer: said once, in words. Without
 * `failedKey` only the cause of a failed read is given (the card has already
 * said that it cannot read). */
export function ReadNote({
  problem,
  formatKey,
  failedKey,
}: {
  problem: ReadProblem;
  formatKey: string;
  failedKey?: string;
}) {
  const { t } = useLocale();
  if (isFormat(problem)) return <p className="rg-bad">{t(formatKey)}</p>;
  return (
    <p className="rg-bad" role="status">
      {failedKey && <>{t(failedKey)} </>}
      <WordedText worded={sayFailure(problem)} />{" "}
      <FailureDetail failure={problem} />
    </p>
  );
}

/** Whether a failure is about the site file: then neither card can work. */
export const isSiteProblem = (problem: ReadProblem | null): boolean =>
  !!problem &&
  !isFormat(problem) &&
  (problem.code === "site_missing" || problem.code === "site_invalid");

/** The site file is missing or invalid: where the page looked, what to fix,
 * and (for a missing file) the template, which is the server's text. */
export function SiteIssue({ failure }: { failure: ApiFailure }) {
  const { t } = useLocale();
  const template = str(failure.extra.template, 8000);
  const errors = list(failure.extra.errors).flatMap((entry) => {
    const item = rec(entry);
    const field = str(item?.field, 120);
    const code = str(item?.code, 60);
    return field && code ? [{ field, code }] : [];
  });
  return (
    <div className="rg-site-issue" data-testid="marvel0-site-issue">
      <Problem
        live={false}
        tone="warning"
        title={t("marvel0.act.title")}
        why={<WordedText worded={sayFailure(failure)} />}
        fix={
          <>
            {errors.length > 0 && (
              <>
                <strong>{t("marvel0.act.site_errors")}</strong>
                <ul className="rg-notes">
                  {errors.map((error) => (
                    <li key={`${error.field}:${error.code}`} lang="en">
                      <code>{error.field}</code>: {error.code}
                    </li>
                  ))}
                </ul>
              </>
            )}
            {template && (
              <details>
                <summary>{t("marvel0.act.template")}</summary>
                <pre lang="en" className="rg-log">
                  {template}
                </pre>
              </details>
            )}
          </>
        }
      />
    </div>
  );
}
