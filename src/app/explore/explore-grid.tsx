// Modified for LEVI (2026); see NOTICE and docs/UPSTREAM.md.
"use client";
import { T } from "@/components/levi-locale";

import React, { useEffect, useRef } from "react";
import Link from "next/link";
import { postParentMessageWithParams } from "@/utils/postParentMessage";
import HfAuthButton from "@/components/hf-auth-button";
import {
  ArrowUpRight,
  ChevronLeft,
  ChevronRight,
  Database,
} from "lucide-react";
import { EmptyState, Icon } from "@/components/ds";
import { Problem } from "@/components/pages-ui/feedback";

type ExploreGridProps = {
  datasets: Array<{ id: string; videoUrl: string | null }>;
  currentPage: number;
  totalPages: number;
  error?: string;
};

export default function ExploreGrid({
  datasets,
  currentPage,
  totalPages,
  error,
}: ExploreGridProps) {
  // sync with parent window hf.co/spaces
  useEffect(() => {
    postParentMessageWithParams((params: URLSearchParams) => {
      params.set("path", window.location.pathname + window.location.search);
    });
  }, []);

  // Create an array of refs for each video
  const videoRefs = useRef<(HTMLVideoElement | null)[]>([]);

  return (
    <T>
      {
        <main className="ds-root pg-workbench">
          <div className="pg-head">
            <h1>
              <T>Explore LeRobot datasets</T>
            </h1>
            <div className="pg-head-actions">
              <Link
                className="ds-btn ds-btn--ghost ds-focus"
                href="/explore?catalog=all"
              >
                <T>Live Hub catalog</T>
                <Icon icon={ArrowUpRight} />
              </Link>
              <HfAuthButton />
            </div>
          </div>
          {error && (
            <Problem
              title={<T>{error}</T>}
              why={<T>The Hugging Face dataset list did not answer.</T>}
              fix={
                <button
                  type="button"
                  className="ds-btn ds-btn--secondary ds-btn--sm ds-focus"
                  onClick={() => window.location.reload()}
                >
                  <T>Try again</T>
                </button>
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
                onMouseEnter={() => {
                  const vid = videoRefs.current[idx];
                  if (vid) void vid.play().catch(() => {});
                }}
                onMouseLeave={() => {
                  const vid = videoRefs.current[idx];
                  if (vid) {
                    vid.pause();
                    vid.currentTime = 0;
                  }
                }}
              >
                <div className="pg-explore-media">
                  <video
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
          <nav className="pg-explore-pager">
            {currentPage > 1 && (
              <button
                type="button"
                className="ds-btn ds-btn--secondary ds-focus"
                onClick={() => {
                  const params = new URLSearchParams(window.location.search);
                  params.set("p", (currentPage - 1).toString());
                  window.location.search = params.toString();
                }}
              >
                <Icon icon={ChevronLeft} />
                <T>Previous page</T>
              </button>
            )}
            {currentPage < totalPages && (
              <button
                type="button"
                className="ds-btn ds-btn--secondary ds-focus"
                onClick={() => {
                  const params = new URLSearchParams(window.location.search);
                  params.set("p", (currentPage + 1).toString());
                  window.location.search = params.toString();
                }}
              >
                <T>Next page</T>
                <Icon icon={ChevronRight} />
              </button>
            )}
          </nav>
        </main>
      }
    </T>
  );
}
