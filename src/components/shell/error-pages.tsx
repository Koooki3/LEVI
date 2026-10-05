"use client";
/**
 * Full-page states for the routes the framework answers itself: an address
 * that does not exist (404) and an error nobody caught. In the frame's own
 * look (token surface, three parts: what happened, why, what to do) and in
 * the reader's language, replacing the framework's default pages. They fill
 * exactly the space below the top bar, so the page does not scroll.
 */
import Link from "next/link";
import type { ReactNode } from "react";
import { Compass, Home, RotateCw, TriangleAlert } from "lucide-react";
import { Button, Card, EmptyState } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";

function Frame({ children, role }: { children: ReactNode; role?: "alert" }) {
  return (
    <main className="levi-error-page" role={role}>
      <Card className="levi-error-page__card">{children}</Card>
    </main>
  );
}

function HomeLink({ primary }: { primary: boolean }) {
  const { t } = useLocale();
  return (
    <Link
      href="/"
      className={`ds-btn ds-btn--${primary ? "primary" : "secondary"} ds-focus`}
    >
      <Home className="ds-icon" width={16} height={16} aria-hidden="true" />
      {t("Back to the home page")}
    </Link>
  );
}

/** The 404 page. */
export function NotFoundPage() {
  const { t } = useLocale();
  return (
    <Frame>
      <EmptyState
        icon={Compass}
        title={t("This page does not exist")}
        description={t(
          "The address may be mistyped, or the dataset or episode it names may have been removed or renamed. Start from the home page or Explore.",
        )}
        action={<HomeLink primary />}
        secondaryAction={
          <Link href="/explore" className="ds-btn ds-btn--secondary ds-focus">
            {t("Explore")}
          </Link>
        }
      />
    </Frame>
  );
}

/** The error nobody caught: what happened, why (the message, collapsed),
 * what to do (try again, or leave). */
export function RouteErrorPage({
  message,
  onRetry,
}: {
  message: string;
  onRetry: () => void;
}) {
  const { t } = useLocale();
  return (
    <Frame role="alert">
      <EmptyState
        icon={TriangleAlert}
        title={t("Something went wrong on this page")}
        description={t(
          "LEVI hit an error while showing this page. Your data is untouched. Try again; if it keeps happening, go back to the home page and open the page again.",
        )}
        action={
          <Button variant="primary" icon={RotateCw} onClick={onRetry}>
            {t("Try again")}
          </Button>
        }
        secondaryAction={<HomeLink primary={false} />}
      />
      {message && (
        <details className="levi-error-page__details">
          <summary>{t("Technical details")}</summary>
          <pre>{message}</pre>
        </details>
      )}
    </Frame>
  );
}
