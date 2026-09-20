"use client";
import { useEffect, useState } from "react";
import { usePathname } from "next/navigation";
import type { ObjectAnnotation } from "@/types/object-annotation.types";
type Window = {
  second: number;
  rows: ObjectAnnotation[];
  ledger: { frame: number; timestamp: number }[];
};
export function useAgentMaskPreview(camera: string, timestamp: number) {
  const [options, setOptions] = useState<{
    opacity: number;
    outline: boolean;
    hidden: number[];
  }>({ opacity: 1, outline: false, hidden: [] });
  useEffect(() => {
    function update(e: Event) {
      setOptions((e as CustomEvent).detail);
    }
    globalThis.window.addEventListener("levi-agent-mask-options", update);
    return () =>
      globalThis.window.removeEventListener("levi-agent-mask-options", update);
  }, []);
  const pathname = usePathname();
  const second = Math.floor(timestamp);
  const [job, setJob] = useState<{ job_id: string; episode: number } | null>(
    null,
  );
  const [window, setWindow] = useState<Window | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    function select(e: Event) {
      const detail = (e as CustomEvent).detail;
      const parts = pathname.split("/").filter(Boolean);
      if (
        detail &&
        parts.slice(0, 2).map(decodeURIComponent).join("/") ===
          detail.repo_id &&
        Number(parts[2]?.replace("episode_", "")) === detail.episode
      ) {
        setJob(detail);
        setWindow(null);
      } else {
        setJob(null);
        setWindow(null);
      }
    }
    globalThis.window.addEventListener("levi-agent-preview", select);
    return () =>
      globalThis.window.removeEventListener("levi-agent-preview", select);
  }, [pathname]);
  useEffect(() => {
    setJob(null);
    setWindow(null);
  }, [pathname]);
  useEffect(() => {
    if (!job) return;
    const abort = new AbortController();
    void fetch("/api/levi/agent/v1/tools", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: "objects.frame",
        arguments: {
          job_id: job.job_id,
          episode: job.episode,
          camera,
          timestamp: second,
          window_seconds: 1,
        },
      }),
      signal: abort.signal,
    })
      .then(async (r) => {
        if (!r.ok) throw new Error("Draft masks unavailable");
        return r.json();
      })
      .then((value) => {
        setWindow({ second, rows: value.rows, ledger: value.ledger || [] });
        setError("");
      })
      .catch((e) => {
        if (e.name !== "AbortError") {
          setWindow(null);
          setError("Draft masks unavailable");
        }
      });
    return () => abort.abort();
  }, [job, camera, second]);
  const ledger =
    window?.second === second
      ? window.ledger.filter((row) => row.timestamp <= timestamp)
      : [];
  const frame = ledger[ledger.length - 1]?.frame;
  return {
    options,
    cacheKey: window,
    rows:
      window?.second === second
        ? window.rows.filter((row) => row.frame_index === frame)
        : [],
    error,
    active: !!job,
  };
}
