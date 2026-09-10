// Modified for LEVI (2026); see NOTICE and docs/UPSTREAM.md.
"use client";
import { T } from "@/components/levi-locale";

import React from "react";

export default function Error({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <T>
      {
        <div className="flex h-screen items-center justify-center bg-slate-950 text-red-400">
          <div className="max-w-xl p-8 rounded bg-slate-900 border border-red-500 shadow-lg">
            <h2 className="text-2xl font-bold mb-4">
              <T>Something went wrong</T>
            </h2>
            <p className="text-lg font-mono whitespace-pre-wrap mb-4">
              <T>{error.message}</T>
            </p>
            <button
              className="mt-4 px-4 py-2 bg-red-500 text-white rounded hover:bg-red-600"
              onClick={() => reset()}
            >
              <T>Try Again</T>
            </button>
          </div>
        </div>
      }
    </T>
  );
}
