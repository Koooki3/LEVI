"use client";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type MouseEvent,
} from "react";
import { useLocale } from "@/components/levi-locale";
import {
  EXPORT_FORMATS,
  STATS_BINDING_SCHEMA,
  exportHref,
  type ExportFormat,
  type StatsScope,
  type StatsSourceIdentity,
} from "./stats-logic";

export function SessionStatisticsExport({
  scope,
  sources,
  onError,
  onSuccess,
}: {
  scope: StatsScope;
  sources: readonly StatsSourceIdentity[];
  onError: (message: string) => void;
  onSuccess?: () => void;
}) {
  const { t, language } = useLocale();
  const [busy, setBusy] = useState(false);
  const generation = useRef(0);
  const controller = useRef<AbortController | null>(null);
  const key = exportHref("json", scope, language, sources);
  const currentKey = useRef(key);
  currentKey.current = key;
  const revoke = useCallback(() => {
    ++generation.current;
    controller.current?.abort();
  }, []);
  useEffect(() => {
    revoke();
    setBusy(false);
    return revoke;
  }, [key, revoke]);
  const download = async (
    event: MouseEvent<HTMLAnchorElement>,
    format: ExportFormat,
  ) => {
    event.preventDefault();
    if (busy) return;
    const mine = ++generation.current;
    const requestKey = key;
    const abort = new AbortController();
    controller.current?.abort();
    controller.current = abort;
    const valid = () =>
      !abort.signal.aborted &&
      mine === generation.current &&
      currentKey.current === requestKey;
    setBusy(true);
    try {
      const response = await fetch(
        exportHref(format, scope, language, sources),
        { cache: "no-store", signal: abort.signal },
      );
      if (!valid()) return;
      if (
        response.status === 409 ||
        (response.ok &&
          response.headers.get("X-LEVI-Stats-Source-Binding") !==
            STATS_BINDING_SCHEMA)
      ) {
        onError(
          "Statistics source changed. Refresh this session before downloading.",
        );
        return;
      }
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const blob = await response.blob();
      if (!valid()) return;
      const href = URL.createObjectURL(blob);
      try {
        const anchor = document.createElement("a");
        anchor.href = href;
        anchor.download = `live-stats.${format}`;
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
        onSuccess?.();
      } finally {
        URL.revokeObjectURL(href);
      }
    } catch {
      if (valid())
        onError(
          "Could not download statistics. Refresh this session and try again.",
        );
    } finally {
      if (valid()) setBusy(false);
    }
  };
  return (
    <p className="pg-live-export" aria-busy={busy}>
      {t("Download")}:{" "}
      {EXPORT_FORMATS.map((format, index) => (
        <span key={format}>
          {index > 0 && " · "}
          <a
            href={exportHref(format, scope, language, sources)}
            download
            aria-disabled={busy}
            onClick={(event) => {
              void download(event, format);
            }}
          >
            {format === "md" ? "Markdown" : format.toUpperCase()}
          </a>
        </span>
      ))}
    </p>
  );
}
