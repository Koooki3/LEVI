// Modified for LEVI (2026); see NOTICE and docs/UPSTREAM.md.
"use client";
import { T } from "@/components/levi-locale";

export default function Loading() {
  return (
    <T>
      {
        <div
          className="absolute inset-0 flex flex-col items-center justify-center bg-[var(--bg)]/80 backdrop-blur-sm z-10 text-slate-200"
          tabIndex={-1}
          aria-modal="true"
          role="dialog"
        >
          <svg
            className="animate-spin mb-5 text-cyan-300"
            width="42"
            height="42"
            viewBox="0 0 24 24"
            fill="none"
            xmlns="http://www.w3.org/2000/svg"
          >
            <circle
              className="opacity-15"
              cx="12"
              cy="12"
              r="10"
              stroke="currentColor"
              strokeWidth="3"
            />
            <path
              className="opacity-80"
              fill="currentColor"
              d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
            />
          </svg>
          <h1 className="text-sm font-medium tracking-wide uppercase text-slate-300">
            <T>Loading</T>
          </h1>
          <p className="text-xs text-slate-500 mt-1">
            <T>preparing data &amp; videos</T>
          </p>
        </div>
      }
    </T>
  );
}
