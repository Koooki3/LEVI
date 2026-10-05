"use client";
// The last-resort boundary: it replaces the root layout, so it brings its own
// <html>, the token sheets and no providers (no language catalogue: the
// words are both languages at once).
import "@/styles/tokens.css";
import "@/styles/ds.css";
import "@/styles/shell.css";
import "./globals.css";
import { useEffect, useRef } from "react";

export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  const headline = useRef<HTMLSpanElement>(null);
  useEffect(() => headline.current?.focus(), []);
  return (
    <html lang="en">
      <body>
        <main className="levi-error-page levi-error-page--bare">
          <div className="ds-card ds-card--default ds-card--regular levi-error-page__card">
            <div className="ds-empty">
              <p className="ds-empty__title">
                <span
                  ref={headline}
                  tabIndex={-1}
                  className="levi-error-page__title"
                >
                  <span lang="en">Something went wrong</span> ·{" "}
                  <span lang="zh">出错了</span>
                </span>
              </p>
              <p className="ds-empty__description" role="alert">
                <span lang="en">
                  LEVI could not show this page. Reload it; your data is
                  untouched.
                </span>{" "}
                ·{" "}
                <span lang="zh">
                  LEVI 无法显示此页面。请重新加载；你的数据没有受到影响。
                </span>
              </p>
              <div className="ds-empty__actions">
                <button
                  type="button"
                  className="ds-btn ds-btn--primary ds-focus"
                  onClick={reset}
                >
                  <span lang="en">Try again</span> · <span lang="zh">重试</span>
                </button>
              </div>
            </div>
            {error.message && (
              <details className="levi-error-page__details">
                <summary>
                  <span lang="en">Technical details</span> ·{" "}
                  <span lang="zh">技术细节</span>
                </summary>
                <pre>{error.message}</pre>
              </details>
            )}
          </div>
        </main>
      </body>
    </html>
  );
}
