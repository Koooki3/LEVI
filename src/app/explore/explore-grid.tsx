// Modified for LEVI (2026); see NOTICE and docs/UPSTREAM.md.
"use client";
import { T, useLocale } from "@/components/levi-locale";

import React, { useEffect, useRef } from "react";
import Link from "next/link";
import { postParentMessageWithParams } from "@/utils/postParentMessage";
import HfAuthButton from "@/components/hf-auth-button";
import { ChevronLeft, ChevronRight, Database } from "lucide-react";
import { EmptyState, Icon } from "@/components/ds";
import { Problem } from "@/components/pages-ui/feedback";
import { OpenDataset } from "@/components/home/open-dataset";
import { usePrefersReducedMotion } from "@/lib/design/motion";

type ExploreGridProps = {
  datasets: Array<{ id: string; videoUrl: string | null }>;
  currentPage: number;
  totalPages: number;
  error?: string;
  /** Where the list comes from: the curated examples or the whole Hub. */
  catalog?: "examples" | "all";
};

const ALL_HREF = "/explore?catalog=all";

export default function ExploreGrid({
  datasets,
  currentPage,
  totalPages,
  error,
  catalog = "examples",
}: ExploreGridProps) {
  const { t } = useLocale();
  // A preview that starts by itself on hover or focus is motion the reader
  // asked not to get when the system or the app setting says "reduced".
  const reduceMotion = usePrefersReducedMotion();
  // sync with parent window hf.co/spaces
  useEffect(() => {
    postParentMessageWithParams((params: URLSearchParams) => {
      params.set("path", window.location.pathname + window.location.search);
    });
  }, []);

  // Create an array of refs for each video
  const videoRefs = useRef<(HTMLVideoElement | null)[]>([]);
  const startPreview = (idx: number) => {
    const vid = videoRefs.current[idx];
    if (vid && !reduceMotion) void vid.play().catch(() => {});
  };
  const stopPreview = (idx: number) => {
    const vid = videoRefs.current[idx];
    if (vid) {
      vid.pause();
      vid.currentTime = 0;
    }
  };
  const pageHref = (page: number) => `${ALL_HREF}&p=${page}`;

  return (
    <T>
      {
        <main id="levi-main" className="ds-root ds-page pg-workbench">
          <div className="pg-head">
            <h1>
              <T>Explore LeRobot datasets</T>
            </h1>
            <div className="pg-head-actions">
              <nav
                className="ds-segmented ds-segmented--sm pg-explore-source"
                aria-label={t("Dataset source")}
              >
                <Link
                  href="/explore"
                  className="ds-segment ds-focus"
                  aria-current={catalog === "examples" ? "page" : undefined}
                >
                  <T>Examples</T>
                </Link>
                <Link
                  href={ALL_HREF}
                  className="ds-segment ds-focus"
                  aria-current={catalog === "all" ? "page" : undefined}
                >
                  <T>All Hub datasets</T>
                </Link>
              </nav>
              <HfAuthButton />
            </div>
          </div>
          <div className="pg-explore-search">
            <OpenDataset />
            <p className="pg-small">
              <T>
                Search the whole Hub above, or start from a public example that
                streams on demand.
              </T>
            </p>
          </div>
          {error && (
            <Problem
              title={<T>{error}</T>}
              why={<T>The Hugging Face dataset list did not answer.</T>}
              fix={
                <>
                  <button
                    type="button"
                    className="ds-btn ds-btn--secondary ds-btn--sm ds-focus"
                    onClick={() => window.location.reload()}
                  >
                    <T>Try again</T>
                  </button>
                  {catalog === "all" && (
                    <Link
                      href="/explore"
                      className="ds-btn ds-btn--ghost ds-btn--sm ds-focus"
                    >
                      <T>Show the example datasets</T>
                    </Link>
                  )}
                </>
              }
            />
          )}
          {!error && datasets.length === 0 && (
            <EmptyState icon={Database} title={<T>No datasets to show.</T>} />
          )}
          <div className="pg-explore-grid">
            {datasets.map((ds, idx) => (
              <Link
                key={ds.id}
                href={`/${ds.id}`}
                className="pg-explore-card ds-focus"
                aria-label={ds.id}
                onMouseEnter={() => startPreview(idx)}
                onMouseLeave={() => stopPreview(idx)}
                onFocus={() => startPreview(idx)}
                onBlur={() => stopPreview(idx)}
              >
                <div className="pg-explore-media">
                  <video
                    aria-hidden="true"
                    ref={(el) => {
                      videoRefs.current[idx] = el;
                    }}
                    src={ds.videoUrl || undefined}
                    loop
                    muted
                    playsInline
                    preload="metadata"
                    onTimeUpdate={(e) => {
                      const vid = e.currentTarget;
                      if (vid.currentTime >= 15) {
                        vid.pause();
                        vid.currentTime = 0;
                      }
                    }}
                  />
                </div>
                <div className="pg-explore-name">
                  <T>{ds.id}</T>
                </div>
              </Link>
            ))}
          </div>
          {totalPages > 1 && (
            <nav className="pg-explore-pager" aria-label={t("Dataset pages")}>
              {currentPage > 1 ? (
                <Link
                  href={pageHref(currentPage - 1)}
                  className="ds-btn ds-btn--secondary ds-focus"
                  rel="prev"
                >
                  <Icon icon={ChevronLeft} />
                  <T>Previous page</T>
                </Link>
              ) : null}
              <span className="pg-explore-pager__where">
                {t("Page {page} of {pages}")
                  .replace("{page}", String(currentPage))
                  .replace("{pages}", String(totalPages))}
              </span>
              {currentPage < totalPages ? (
                <Link
                  href={pageHref(currentPage + 1)}
                  className="ds-btn ds-btn--secondary ds-focus"
                  rel="next"
                >
                  <T>Next page</T>
                  <Icon icon={ChevronRight} />
                </Link>
              ) : null}
            </nav>
          )}
        </main>
      }
    </T>
  );
}
