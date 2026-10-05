"use client";
// The technical report page (/report): LEVI_REPORT_DIR rendered live.
import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  Clock,
  Cpu,
  FileQuestion,
  GitCommitHorizontal,
  HardDrive,
  List,
  Radio,
  RotateCcw,
  WifiOff,
} from "lucide-react";
import {
  Button,
  Card,
  EmptyState,
  Icon,
  SkeletonText,
  Tooltip,
  useToast,
} from "@/components/ds";
import { leviApi } from "@/components/levi-api";
import { Problem } from "@/components/pages-ui/feedback";
import { LeviMark } from "@/components/shell/brand";
import { useLocale } from "@/components/levi-locale";
import {
  type Heading,
  type ReportLang,
  type ReportPayload,
  assetSrc,
  extractHeadings,
  formatAge,
  formatGpu,
  formatStamp,
  isFollowableLink,
  isReportBlock,
  workstreamLabel,
} from "@/utils/report";
import { currentHeadingIndex, readingLine, stickyTop } from "./active-section";
import { ReportBlock, ReportContext } from "./report-blocks";

const POLL_MS = 5000;

type HastNode = {
  type: string;
  tagName?: string;
  value?: string;
  properties?: { className?: unknown };
  children?: HastNode[];
  position?: { start: { line: number } };
};

function textOf(node: HastNode | undefined): string {
  if (!node) return "";
  if (node.type === "text") return node.value ?? "";
  return (node.children ?? []).map(textOf).join("");
}

function codeLanguage(node: HastNode | undefined): string | null {
  const names = node?.properties?.className;
  const list = Array.isArray(names) ? names : [];
  const found = list.find(
    (name): name is string =>
      typeof name === "string" && name.startsWith("language-"),
  );
  return found ? found.slice("language-".length) : null;
}

/** The Markdown alone; re-parsed only when the document text changes. */
const ReportMarkdown = memo(function ReportMarkdown({
  markdown,
  headings,
}: {
  markdown: string;
  headings: Heading[];
}) {
  const { t } = useLocale();
  const tableLabel = t("report.table");
  const components = useMemo<Components>(() => {
    const ids = new Map(headings.map((h) => [h.line, h.id]));
    const id = (node: unknown) => {
      const line = (node as HastNode | undefined)?.position?.start.line;
      return line ? ids.get(line) : undefined;
    };
    return {
      h1: ({ node, children }) => <h1 id={id(node)}>{children}</h1>,
      h2: ({ node, children }) => <h2 id={id(node)}>{children}</h2>,
      h3: ({ node, children }) => <h3 id={id(node)}>{children}</h3>,
      h4: ({ node, children }) => <h4 id={id(node)}>{children}</h4>,
      h5: ({ node, children }) => <h5 id={id(node)}>{children}</h5>,
      h6: ({ node, children }) => <h6 id={id(node)}>{children}</h6>,
      pre({ node, children }) {
        const code = (node as HastNode | undefined)?.children?.find(
          (child) => child.type === "element" && child.tagName === "code",
        );
        const language = codeLanguage(code);
        if (isReportBlock(language))
          return <ReportBlock language={language} source={textOf(code)} />;
        return <pre className="lr-pre">{children}</pre>;
      },
      img({ src, alt }) {
        const url = assetSrc(typeof src === "string" ? src : null);
        if (!url)
          return (
            <span className="lr-missing-image" title={String(src ?? "")}>
              {alt || String(src ?? "")}
            </span>
          );
        // eslint-disable-next-line @next/next/no-img-element
        return <img src={url} alt={alt ?? ""} loading="lazy" />;
      },
      a({ href, children }) {
        if (!isFollowableLink(href))
          return (
            <span className="lr-ref" title={href}>
              {children}
            </span>
          );
        const external = /^https?:/i.test(href ?? "");
        return (
          <a
            href={href}
            {...(external
              ? { target: "_blank", rel: "noopener noreferrer" }
              : {})}
          >
            {children}
          </a>
        );
      },
      table({ children }) {
        return (
          // A wide table scrolls sideways: the box must take focus to scroll
          // from the keyboard, and have a name.
          <div
            className="lr-table-wrap"
            tabIndex={0}
            role="region"
            aria-label={tableLabel}
          >
            <table className="lr-table lr-md-table">{children}</table>
          </div>
        );
      },
    };
  }, [headings, tableLabel]);
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
      {markdown}
    </ReactMarkdown>
  );
});

function Toc({ headings, active }: { headings: Heading[]; active: string }) {
  const { t } = useLocale();
  const items = headings.filter((h) => h.level >= 2 && h.level <= 3);
  if (!items.length) return null;
  return (
    <nav className="levi-toc" aria-label={t("report.contents")}>
      <div className="levi-toc__title">
        <Icon icon={List} />
        {t("report.contents")}
      </div>
      <ol>
        {items.map((h) => (
          <li key={h.id} className={`lr-toc-l${h.level}`}>
            <a
              href={`#${h.id}`}
              aria-current={active === h.id ? "location" : undefined}
            >
              {h.text}
            </a>
          </li>
        ))}
      </ol>
    </nav>
  );
}

function useActiveHeading(headings: Heading[]) {
  const [active, setActive] = useState("");
  useEffect(() => {
    if (!headings.length) return;
    const marked = headings.filter((h) => h.level >= 2 && h.level <= 3);
    const onScroll = () => {
      const tops = marked.map(
        (h) =>
          document.getElementById(h.id)?.getBoundingClientRect().top ?? null,
      );
      const index = currentHeadingIndex(
        tops,
        readingLine(window.innerHeight, stickyTop()),
      );
      setActive(index >= 0 ? marked[index].id : "");
    };
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, [headings]);
  return active;
}

function ReportEmpty({ report }: { report: ReportPayload }) {
  const { t } = useLocale();
  const [title, body] = !report.configured
    ? ["report.empty.unconfigured", "report.empty.unconfiguredBody"]
    : !report.exists
      ? ["report.empty.missingDir", "report.empty.missingDirBody"]
      : ["report.empty.missingDoc", "report.empty.missingDocBody"];
  return (
    <Card className="lr-empty">
      <EmptyState icon={FileQuestion} title={t(title)} description={t(body)} />
      {report.dir && <pre className="lr-pre">{report.dir}</pre>}
      <pre className="lr-pre">
        LEVI_REPORT_DIR=/path/to/report{"\n"}
        {"  "}LEVI.md{"\n"}
        {"  "}LEVI.zh-CN.md{"\n"}
        {"  "}status.json{"\n"}
        {"  "}assets/
      </pre>
    </Card>
  );
}

function StatusStrip({
  report,
  live,
  now,
}: {
  report: ReportPayload;
  live: boolean;
  now: number;
}) {
  const { t } = useLocale();
  const lang = report.lang;
  const status = report.status;
  const gpu = formatGpu(status?.resources);
  // The GPU holder is recorded by workstream id; show its title.
  const holder = workstreamLabel(status, gpu?.holder, lang);
  const disk = status?.resources?.disk_free_gb;
  const age = formatAge(status?.generated_at, now, lang);
  return (
    <div className="lr-strip" aria-live="off">
      <Tooltip
        content={t(live ? "report.liveHint" : "report.offlineHint")}
        placement="bottom"
      >
        <span
          className={`lr-live ${live ? "lr-live-on" : "lr-live-off"}`}
          role="status"
        >
          <Icon icon={live ? Radio : WifiOff} />
          {t(live ? "report.live" : "report.offline")}
          {/* The hint is a hover tooltip for the pointer; the words are here
              for everyone else (the badge is not a control to focus). */}
          <span className="ds-sr-only">
            {" — "}
            {t(live ? "report.liveHint" : "report.offlineHint")}
          </span>
        </span>
      </Tooltip>
      {status?.generated_at && (
        <span className="lr-strip-item" title={status.generated_at}>
          <Icon icon={Clock} />
          <span className="lr-strip-key">{t("report.generated")}</span>
          {formatStamp(status.generated_at)}
          {age && <span className="lr-faint"> · {age}</span>}
        </span>
      )}
      {status?.levi_main && (
        <span className="lr-strip-item">
          <Icon icon={GitCommitHorizontal} />
          <span className="lr-strip-key">{t("report.leviMain")}</span>
          <code>{status.levi_main}</code>
        </span>
      )}
      {gpu && (
        <span className="lr-strip-item">
          <Icon icon={Cpu} />
          <span className="lr-strip-key">GPU</span>
          <span className="lr-mini-bar" aria-hidden="true">
            <span style={{ width: `${Math.round(gpu.fraction * 100)}%` }} />
          </span>
          {gpu.text}
          <span className="lr-faint" title={holder.id ?? undefined}>
            {" · "}
            {holder.text || t("report.gpuFree")}
          </span>
        </span>
      )}
      {typeof disk === "number" && Number.isFinite(disk) && (
        <span className="lr-strip-item">
          <Icon icon={HardDrive} />
          <span className="lr-strip-key">{t("report.diskFree")}</span>
          {disk >= 1000
            ? `${(disk / 1000).toFixed(2)} TB`
            : `${Math.round(disk)} GB`}
        </span>
      )}
    </div>
  );
}

export default function ReportView() {
  const { language, t } = useLocale();
  const lang: ReportLang = language === "zh" ? "zh" : "en";
  const [report, setReport] = useState<ReportPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [live, setLive] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const [toast, setToast] = useState(false);
  const etag = useRef<string | null>(null);
  const shownLang = useRef<ReportLang | null>(null);
  const wantedLang = useRef<ReportLang>(lang);
  useEffect(() => {
    wantedLang.current = lang;
  }, [lang]);

  const load = useCallback(
    async (announce: boolean) => {
      const next = await leviApi<ReportPayload>(`report?lang=${lang}`);
      // A slower answer for the language the reader just left is dropped.
      if (wantedLang.current !== lang) return;
      etag.current = next.etag;
      const changed = shownLang.current === lang;
      shownLang.current = lang;
      setReport(next);
      setError(null);
      setLive(true);
      if (announce && changed) setToast(true);
    },
    [lang],
  );

  // The first read, and "Try again" after it failed.
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let stopped = false;
    load(false).catch((e: unknown) => {
      if (stopped) return;
      setError(e instanceof Error ? e.message : String(e));
      setLive(false);
    });
    const timer = window.setInterval(async () => {
      setNow(Date.now());
      if (document.hidden) return;
      try {
        const marker = await leviApi<{ etag: string }>(
          `report/version?lang=${lang}`,
        );
        if (stopped) return;
        setLive(true);
        if (marker.etag !== etag.current) await load(true);
      } catch {
        if (!stopped) setLive(false);
      }
    }, POLL_MS);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [lang, load, attempt]);

  // "Updated": a note in the frame's toast region (polite, hides itself).
  const toasts = useToast();
  useEffect(() => {
    if (!toast) return;
    toasts.show({ title: t("report.updated") });
    setToast(false);
  }, [toast, toasts, t]);

  const markdown = report?.markdown ?? null;
  const headings = useMemo(
    () => (markdown ? extractHeadings(markdown) : []),
    [markdown],
  );
  const active = useActiveHeading(headings);
  const context = useMemo(
    () => ({ status: report?.status ?? null, lang, now }),
    [report?.status, lang, now],
  );

  return (
    <main className="lr-page">
      <div className="lr-top">
        <span className="lr-brand">
          <LeviMark size={18} />
          <span className="ds-eyebrow">{t("report.eyebrow")}</span>
        </span>
        {report && <StatusStrip report={report} live={live} now={now} />}
      </div>
      {error && !report && (
        // The same three-part error as the pages: what happened, why (in the
        // page's language; the raw answer under "Technical details"), what to do.
        <Problem
          className="lr-empty"
          title={t("report.unavailable")}
          why={t("The LEVI service did not answer, or answered with an error.")}
          details={error}
          fix={
            <>
              {t("report.unavailableFix")}
              <Button
                size="sm"
                icon={RotateCcw}
                onClick={() => {
                  setError(null);
                  setAttempt((n) => n + 1);
                }}
              >
                {t("Try again")}
              </Button>
            </>
          }
        />
      )}
      {!report && !error && (
        <div className="lr-loading" role="status" aria-busy="true">
          <span className="ds-sr-only">{t("report.loading")}</span>
          <SkeletonText lines={6} />
        </div>
      )}
      {report && report.errors.length > 0 && (
        <div className="lr-block-error" role="alert">
          <strong>{t("report.fileErrors")}</strong>
          <ul>
            {report.errors.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </div>
      )}
      {report && !markdown && <ReportEmpty report={report} />}
      {report && markdown && (
        <div className="levi-reading__layout">
          <aside className="levi-reading__aside">
            <Toc headings={headings} active={active} />
          </aside>
          <article className="lr-doc levi-prose">
            {report.document_lang && report.document_lang !== lang && (
              <p className="lr-note">{t("report.fallbackEnglish")}</p>
            )}
            <ReportContext.Provider value={context}>
              <ReportMarkdown markdown={markdown} headings={headings} />
            </ReportContext.Provider>
          </article>
        </div>
      )}
    </main>
  );
}
