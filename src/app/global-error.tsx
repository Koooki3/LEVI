"use client";
// The last-resort boundary: it replaces the root layout, so it brings its own
// <html>, the token sheets and no providers (no language catalogue: the
// words are both languages at once).
import "@/styles/tokens.css";
import "@/styles/ds.css";
import "@/styles/shell.css";
import "./globals.css";

export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <html lang="en">
      <body>
        <main className="levi-error-page levi-error-page--bare" role="alert">
          <div className="ds-card ds-card--default ds-card--regular levi-error-page__card">
            <div className="ds-empty">
              <p className="ds-empty__title">Something went wrong · 出错了</p>
              <p className="ds-empty__description">
                LEVI could not show this page. Reload it; your data is
                untouched. · LEVI
                无法显示此页面。请重新加载；你的数据没有受到影响。
              </p>
              <div className="ds-empty__actions">
                <button
                  type="button"
                  className="ds-btn ds-btn--primary ds-focus"
                  onClick={reset}
                >
                  Try again · 重试
                </button>
              </div>
            </div>
            {error.message && (
              <details className="levi-error-page__details">
                <summary>Technical details · 技术细节</summary>
                <pre>{error.message}</pre>
              </details>
            )}
          </div>
        </main>
      </body>
    </html>
  );
}
