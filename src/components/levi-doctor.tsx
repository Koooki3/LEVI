"use client";
import { useState } from "react";
import { T, useLocale } from "./levi-locale";
import {
  ArrowUpRight,
  Braces,
  Download,
  Flag,
  ListChecks,
  Stethoscope,
} from "lucide-react";
import {
  Badge,
  Button,
  Card,
  Checkbox,
  EmptyState,
  Field,
  Icon,
  Input,
  type Tone,
} from "@/components/ds";
import "@/components/viewer/viewer.css";

const STATUS_TONE: Record<string, Tone> = {
  pass: "success",
  warn: "warning",
  fail: "danger",
};
const STATUS_WORD: Record<string, string> = {
  pass: "Pass",
  warn: "Warn",
  fail: "Fail",
};
import { leviApi, downloadJson, exportName } from "./levi-api";
import { useFlaggedEpisodes } from "@/context/flagged-episodes-context";
import { RawCaptureNotice } from "@/components/raw-capture-notice";
const CHECKS = [
  "metadata",
  "temporal",
  "action",
  "video",
  "distribution",
  "episodes",
  "features",
  "training",
  "anomaly",
  "portability",
];
type Report = {
  episodes: number;
  frames: number;
  fps: number;
  status: string;
  scope: string;
  counts: Record<string, number>;
  results: {
    check: string;
    status: string;
    message: string;
    episode: number | null;
  }[];
  flagged_episodes: number[];
  method: string;
};

export default function LeviDoctor({ repoId }: { repoId: string }) {
  const [checks, setChecks] = useState(CHECKS);
  // A local dataset is checked in full, with video decoded, by default, so a
  // PASS here means what quality.inspect's PASS means. Hub datasets keep a
  // sample: analysing them downloads data.
  const local = repoId.startsWith("local/");
  const [max, setMax] = useState(local ? 0 : 20);
  const [decode, setDecode] = useState(local);
  const [report, setReport] = useState<Report | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [raw, setRaw] = useState(false);
  const { addMany } = useFlaggedEpisodes();
  const { t } = useLocale();
  async function run() {
    setBusy(true);
    setError("");
    try {
      setReport(
        await leviApi<Report>("diagnostics", {
          repo_id: repoId,
          max_episodes: max,
          checks,
          decode_video: decode,
        }),
      );
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }
  const statusBadge = (status: string) => (
    <Badge tone={STATUS_TONE[status] ?? "neutral"}>
      {t(STATUS_WORD[status] ?? status)}
    </Badge>
  );
  return (
    <section className="vw-a-view">
      <RawCaptureNotice feature="doctor" />
      <Card
        title={t("Dataset quality diagnostics")}
        description={t(
          "Version-aware local checks for timestamps, actions, videos and metadata. Reports identify review candidates; thresholds are configurable heuristics.",
        )}
      >
        <div className="flex flex-col gap-4">
          <div className="flex flex-wrap items-end gap-4">
            <code className="vw-code grow">{repoId}</code>
            <Field label={t("Max episodes (0 = all)")}>
              <Input
                className="w-28"
                type="number"
                min="0"
                max="10000"
                value={max}
                onChange={(e) => setMax(Number(e.target.value))}
              />
            </Field>
          </div>
          <fieldset className="vw-checks">
            <legend className="vw-label">{t("Checks")}</legend>
            {CHECKS.map((c) => (
              <Checkbox
                key={c}
                label={t("check." + c)}
                checked={checks.includes(c)}
                onChange={(e) =>
                  setChecks((prev) =>
                    e.target.checked
                      ? [...prev, c]
                      : prev.filter((x) => x !== c),
                  )
                }
              />
            ))}
          </fieldset>
          <Checkbox
            label={t("Decode video samples (downloads remote videos)")}
            description={t(
              "Remote analysis downloads parquet shards. The episode limit applies to analysis, not download size.",
            )}
            checked={decode}
            onChange={(e) => setDecode(e.target.checked)}
          />
          <div className="flex flex-wrap items-center gap-4">
            <Button
              variant="primary"
              icon={Stethoscope}
              loading={busy}
              onClick={run}
              disabled={!checks.length}
              aria-describedby={
                !checks.length ? "doctor-run-reason" : undefined
              }
            >
              {t(busy ? "Running diagnostics…" : "Run diagnostics")}
            </Button>
            {!checks.length && (
              <span id="doctor-run-reason" className="vw-a-hint">
                {t("Choose at least one check to run diagnostics.")}
              </span>
            )}
            {!repoId.startsWith("local/") && (
              <a
                className="vw-link inline-flex items-center gap-1 text-sm"
                target="_blank"
                rel="noreferrer"
                href={`https://jashshah999-lerobot-doctor.hf.space/?dataset=${repoId}`}
              >
                {t("Open upstream lerobot-doctor")}
                <Icon icon={ArrowUpRight} />
              </a>
            )}
          </div>
        </div>
      </Card>
      {error && (
        <div className="vw-note vw-note--danger" role="alert">
          <strong>{t("Diagnostics could not run")}</strong>
          <p>{error}</p>
        </div>
      )}
      {!report && !error && !busy && (
        <EmptyState
          icon={ListChecks}
          title={t("No diagnostics yet")}
          description={t(
            "Choose the checks above and run them; findings can be flagged for review.",
          )}
        />
      )}
      {report && (
        <>
          <div className="flex flex-wrap items-center gap-2">
            {statusBadge(report.status)}
            <span className="vw-muted text-sm">
              <T>Scope</T>: <T>{report.scope}</T>
            </span>
            <span className="grow" />
            <Button size="sm" icon={Braces} onClick={() => setRaw(!raw)}>
              {raw ? t("Report") : "JSON"}
            </Button>
            <Button
              size="sm"
              icon={Download}
              onClick={() =>
                downloadJson(report, exportName(repoId, "quality"))
              }
            >
              {t("Export report")}
            </Button>
            <Button
              size="sm"
              icon={Flag}
              onClick={() => addMany(report.flagged_episodes)}
            >
              {t("Flag findings for review")} ({report.flagged_episodes.length})
            </Button>
          </div>
          <dl className="vw-metrics">
            {[
              ["Episodes", report.episodes],
              ["Frames", report.frames],
              ["Pass", report.counts.pass],
              ["Warn", report.counts.warn],
              ["Fail", report.counts.fail],
            ].map(([k, v]) => (
              <div key={k}>
                <dt>{t(String(k))}</dt>
                <dd>{v}</dd>
              </div>
            ))}
          </dl>
          {raw ? (
            <pre className="vw-pre">{JSON.stringify(report, null, 2)}</pre>
          ) : (
            CHECKS.filter((c) => report.results.some((r) => r.check === c)).map(
              (c) => (
                <Card key={c} title={t("check." + c)} padding="compact">
                  <ul className="vw-findings">
                    {report.results
                      .filter((r) => r.check === c)
                      .map((r, i) => (
                        <li key={i}>
                          {statusBadge(r.status)}
                          {r.episode !== null && (
                            <strong className="tabular">
                              {t(`Episode ${r.episode}`)}
                            </strong>
                          )}
                          <span>{t(r.message)}</span>
                        </li>
                      ))}
                  </ul>
                </Card>
              ),
            )
          )}
          <p className="vw-faint text-xs m-0">{t(report.method)}</p>
        </>
      )}
    </section>
  );
}
