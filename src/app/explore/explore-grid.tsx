// Modified for LEVI (2026); see NOTICE and docs/UPSTREAM.md.
"use client";
import { T } from "@/components/levi-locale";

import React, { useEffect, useRef } from "react";
import Link from "next/link";
import { postParentMessageWithParams } from "@/utils/postParentMessage";
import HfAuthButton from "@/components/hf-auth-button";

type ExploreGridProps = {
  datasets: Array<{ id: string; videoUrl: string | null }>;
  currentPage: number;
  totalPages: number;
};

export default function ExploreGrid({
  datasets,
  currentPage,
  totalPages,
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
        <main className="px-8 py-10 max-w-7xl mx-auto">
          <div className="flex items-center justify-between mb-6 gap-4">
            <h1 className="text-xl font-medium tracking-tight text-slate-100">
              <T>Explore LeRobot datasets</T>
            </h1>
            <div className="flex items-center gap-5">
              <Link
                className="text-xs text-cyan-300"
                href="/explore?catalog=all"
              >
                <T>Live Hub catalog</T> ↗
              </Link>
              <HfAuthButton />
            </div>
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4">
            {datasets.map((ds, idx) => (
              <Link
                key={ds.id}
                href={`/${ds.id}`}
                className="relative rounded-md overflow-hidden h-48 flex items-end group panel hover:border-cyan-400/40 transition-colors"
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
                <video
                  ref={(el) => {
                    videoRefs.current[idx] = el;
                  }}
                  src={ds.videoUrl || undefined}
                  className="absolute top-0 left-0 w-full h-full object-cover object-center z-0"
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
                <div className="absolute inset-0 bg-gradient-to-t from-black/80 via-black/20 to-transparent z-10 pointer-events-none" />
                <div className="relative z-20 w-full px-3 py-2 text-xs text-slate-200 truncate">
                  <T>{ds.id}</T>
                </div>
              </Link>
            ))}
          </div>
          <div className="flex justify-center mt-8 gap-3">
            {currentPage > 1 && (
              <button
                className="px-4 py-2 rounded-md panel text-sm text-slate-300 hover:text-slate-100 hover:bg-white/5 transition-colors"
                onClick={() => {
                  const params = new URLSearchParams(window.location.search);
                  params.set("p", (currentPage - 1).toString());
                  window.location.search = params.toString();
                }}
              >
                <T>‹ Previous</T>
              </button>
            )}
            {currentPage < totalPages && (
              <button
                className="px-4 py-2 rounded-md bg-cyan-400/10 border border-cyan-400/30 text-cyan-300 text-sm hover:bg-cyan-400/15 transition-colors"
                onClick={() => {
                  const params = new URLSearchParams(window.location.search);
                  params.set("p", (currentPage + 1).toString());
                  window.location.search = params.toString();
                }}
              >
                <T>Next ›</T>
              </button>
            )}
          </div>
        </main>
      }
    </T>
  );
}
