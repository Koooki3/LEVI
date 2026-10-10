"use client";
// The campaign report (/automatic/campaigns/<id>/report): the label basis
// switcher, figures F1-F7, the summary text, downloads and the manifest. A
// report of a campaign whose results are still blinded is a 409: the page then
// shows nothing of the results and says why.
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import Markdown from "react-markdown";
import { Download } from "lucide-react";
import { Badge, Button, Field, Select, Skeleton } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { Note, RequestProblem } from "@/components/pages-ui/feedback";
import { FigureCard } from "./campaign-charts";
import { isFigureSpec, specText, type FigureSpec } from "./campaign-figures";
import {
  downloadGroups,
  figureFiles,
  summaryFile,
} from "./campaign-report-logic";
import {
  ApiError,
  reportFileUrl,
  wizardApi,
  type WizardApi,
} from "./wizard-api";
import { IntentKeys, LABEL_BASES } from "./wizard-logic";
import type { CampaignReport } from "./wizard-types";

type ReportApi = Pick<
  WizardApi,
  "getCampaignReport" | "getReportFileJson" | "getReportFileText"
> &
  Partial<Pick<WizardApi, "generateCampaignReport">>;

const GROUP_LABELS: Record<string, string> = {
  tables: "automatic.campaign.report.group.tables",
  figures: "automatic.campaign.report.group.figures",
  data: "automatic.campaign.report.group.data",
  other: "automatic.campaign.report.group.other",
};

type State =
  | { kind: "loading" }
  | { kind: "blinded" }
  | { kind: "missing" }
  | { kind: "error"; message: string }
  | {
      kind: "ready";
      report: CampaignReport;
      figures: FigureSpec[];
      figureErrors: string[];
      summary: string | null;
    };

export function CampaignReportView({
  campaignId,
  basis,
  onBasisChange,
  api = wizardApi,
}: {
  campaignId: string;
  basis: string;
  onBasisChange: (basis: string) => void;
  api?: ReportApi;
}) {
  const { t, language } = useLocale();
  const [state, setState] = useState<State>({ kind: "loading" });
  const generation = useRef(0);
  const keys = useState(() => new IntentKeys())[0];
  const [making, setMaking] = useState(false);
  const [makeProblem, setMakeProblem] = useState<string | null>(null);

  const load = useCallback(async () => {
    const mine = ++generation.current;
    setState({ kind: "loading" });
    try {
      const report = await api.getCampaignReport(campaignId, basis);
      const files = report.files ?? [];
      const figureErrors: string[] = [];
      const figures: FigureSpec[] = [];
      const results = await Promise.all(
        figureFiles(files).map(async (file) => {
          try {
            return {
              file,
              value: await api.getReportFileJson(campaignId, file.name, basis),
            };
          } catch (error) {
            figureErrors.push(
              `${file.name}: ${error instanceof Error ? error.message : String(error)}`,
            );
            return null;
          }
        }),
      );
      for (const r of results)
        if (r) {
          if (isFigureSpec(r.value)) figures.push(r.value);
          else figureErrors.push(`${r.file.name}: not a figure`);
        }
      const summaryName = summaryFile(files, language);
      let summary: string | null = null;
      if (summaryName)
        try {
          summary = await api.getReportFileText(
            campaignId,
            summaryName.name,
            basis,
          );
        } catch {
          summary = null;
        }
      if (mine !== generation.current) return;
      setState({ kind: "ready", report, figures, figureErrors, summary });
    } catch (error) {
      if (mine !== generation.current) return;
      if (
        error instanceof ApiError &&
        (error.status === 409 || error.is("blinded"))
      )
        setState({ kind: "blinded" });
      else if (error instanceof ApiError && error.is("no_report"))
        setState({ kind: "missing" });
      else
        setState({
          kind: "error",
          message: error instanceof Error ? error.message : String(error),
        });
    }
  }, [api, campaignId, basis, language]);

  useEffect(() => {
    void load();
  }, [load]);

  // Make the report of the chosen basis (or make it again, after cards were
  // confirmed): one request id per basis while it runs, then read it back.
  const generate = async () => {
    if (!api.generateCampaignReport || making) return;
    const intent = `report:${basis}`;
    setMaking(true);
    setMakeProblem(null);
    try {
      await api.generateCampaignReport(campaignId, keys.idFor(intent), basis);
      keys.release(intent);
      await load();
    } catch (error) {
      setMakeProblem(error instanceof Error ? error.message : String(error));
      if (error instanceof ApiError && error.status !== 0) keys.release(intent);
    } finally {
      setMaking(false);
    }
  };

  const generateButton = (label: string, variant: "primary" | "secondary") =>
    api.generateCampaignReport ? (
      <div className="pg-row">
        <Button
          variant={variant}
          loading={making}
          disabled={making}
          onClick={() => void generate()}
        >
          {label}
        </Button>
      </div>
    ) : null;

  const analysis =
    state.kind === "ready"
      ? (state.report.analysis as Record<string, unknown>)
      : {};
  const basisName = specText(analysis.basis_name as never, language);
  const automatic = analysis.automatic === true;

  return (
    <>
      <header className="pg-head">
        <h1>{t("automatic.campaign.report.title")}</h1>
        <div className="pg-head-actions">
          <Link
            className="ds-btn ds-btn--secondary ds-btn--md"
            href={`/automatic/campaigns/${encodeURIComponent(campaignId)}`}
          >
            {t("automatic.campaign.report.back")}
          </Link>
        </div>
      </header>
      <p className="pg-pool-muted">
        <code>{campaignId}</code>
      </p>

      <div className="ac-basis">
        <Field label={t("automatic.campaign.report.basis")}>
          <Select
            value={basis}
            onChange={(event) => onBasisChange(event.target.value)}
          >
            {LABEL_BASES.map((b) => (
              <option key={b.value} value={b.value}>
                {t(b.key)}
              </option>
            ))}
          </Select>
        </Field>
        {state.kind === "ready" && basisName && (
          <Badge tone={automatic ? "warning" : "info"}>{basisName}</Badge>
        )}
      </div>

      {state.kind === "loading" && (
        <div role="status" aria-label={t("Loading…")}>
          <Skeleton height={120} />
        </div>
      )}
      {state.kind === "blinded" && (
        <Note tone="info" role="status">
          <strong>{t("automatic.campaign.report.blinded.title")}</strong>{" "}
          {t("automatic.campaign.report.blinded.body")}
        </Note>
      )}
      {state.kind === "missing" && (
        <Note tone="info" role="status">
          <strong>{t("automatic.campaign.report.missing.title")}</strong>{" "}
          {t("automatic.campaign.report.missing.body")}
          {generateButton(
            t("automatic.campaign.report.generate"),
            "primary",
          )}
        </Note>
      )}
      {makeProblem && (
        <RequestProblem
          action="automatic.campaign.report.generate_failed"
          message={makeProblem}
        />
      )}
      {state.kind === "error" && (
        <RequestProblem
          action="automatic.campaign.report.failed"
          message={state.message}
          onRetry={() => void load()}
        />
      )}

      {state.kind === "ready" && (
        <>
          {automatic && (
            <Note tone="warning">
              {t("automatic.campaign.report.automatic_note")}
            </Note>
          )}
          {state.summary && (
            <section
              className="aw-panel ac-summary"
              aria-label={t("automatic.campaign.report.summary")}
            >
              <Markdown>{state.summary}</Markdown>
            </section>
          )}
          <section aria-labelledby="ac-figs">
            <h2 id="ac-figs">{t("automatic.campaign.report.figures")}</h2>
            {state.figures.length === 0 ? (
              <p className="pg-pool-hint">
                {t("automatic.campaign.report.no_figures")}
              </p>
            ) : (
              <div className="ac-figures">
                {state.figures.map((spec) => (
                  <FigureCard key={spec.id} spec={spec} />
                ))}
              </div>
            )}
            {state.figureErrors.length > 0 && (
              <Note tone="warning" role="status">
                {t("automatic.campaign.report.figure_errors")}
                <ul className="aw-list">
                  {state.figureErrors.map((e) => (
                    <li key={e}>
                      <code>{e}</code>
                    </li>
                  ))}
                </ul>
              </Note>
            )}
          </section>

          <section aria-labelledby="ac-files">
            <h2 id="ac-files">{t("automatic.campaign.report.downloads")}</h2>
            {downloadGroups(state.report.files ?? []).map((group) => {
              return (
                <div key={group.key}>
                  <h3>{t(GROUP_LABELS[group.key] ?? GROUP_LABELS.other)}</h3>
                  <ul className="ac-files">
                    {group.files.map((file) => (
                      <li key={file.name}>
                        <a
                          href={reportFileUrl(campaignId, file.name, basis)}
                          download
                        >
                          <Download size={14} aria-hidden="true" /> {file.name}
                        </a>
                      </li>
                    ))}
                  </ul>
                </div>
              );
            })}
          </section>

          {generateButton(
            t("automatic.campaign.report.regenerate"),
            "secondary",
          )}

          <details className="aw-panel ac-manifest">
            <summary>{t("automatic.campaign.report.manifest")}</summary>
            <pre tabIndex={0}>
              {JSON.stringify(state.report.manifest ?? {}, null, 2)}
            </pre>
          </details>
        </>
      )}
    </>
  );
}
