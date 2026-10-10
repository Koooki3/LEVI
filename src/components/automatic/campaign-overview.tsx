"use client";
// The campaign overview (/automatic/campaigns/<id>): progress of every arm,
// the current segment, what needs a person, safety counts and the fuse. While
// the results are blinded it shows codes and counts only, never a success
// rate; revealing them is a deliberate act that counts as a peek.
import Link from "next/link";
import { useRef, useState } from "react";
import { EyeOff, Link2, Pause, Play } from "lucide-react";
import { Badge, Button, Progress, Table } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { Note, RequestProblem } from "@/components/pages-ui/feedback";
import { CampaignCards } from "./campaign-cards";
import { CampaignTodoCard } from "./campaign-todo";
import {
  STATE_TONE,
  formatEta,
  reasonKey,
  stateKey,
  viewOf,
  type CampaignView,
} from "./campaign-logic";
import { usePolled } from "./campaign-poll";
import { PhraseConfirm } from "./wizard-confirm";
import { wizardApi, type WizardApi } from "./wizard-api";
import { codeKey, failureText } from "./wizard-errors";
import { IntentKeys } from "./wizard-logic";

type OverviewApi = Pick<
  WizardApi,
  | "getCampaign"
  | "confirmCampaign"
  | "campaignCommand"
  | "attachCampaign"
  | "getCampaignCards"
  | "confirmCampaignCard"
>;

function ArmsTable({
  view,
  showCards,
}: {
  view: CampaignView;
  showCards: boolean;
}) {
  const { t } = useLocale();
  return (
    <Table caption={t("automatic.campaign.arms.title")} density="compact">
      <thead>
        <tr>
          <th scope="col">{t("automatic.campaign.arms.arm")}</th>
          <th scope="col">{t("automatic.campaign.arms.progress")}</th>
          <th scope="col">{t("automatic.campaign.arms.done")}</th>
          <th scope="col">{t("automatic.campaign.arms.remaining")}</th>
          <th scope="col">{t("automatic.campaign.arms.deviated")}</th>
          <th scope="col">{t("automatic.campaign.arms.discarded")}</th>
          {showCards && (
            <th scope="col">{t("automatic.campaign.arms.unconfirmed")}</th>
          )}
        </tr>
      </thead>
      <tbody>
        {view.arms.map((arm) => (
          <tr key={arm.code} data-current={arm.code === view.segment.armCode}>
            <th scope="row">
              {arm.code}
              {arm.id ? ` · ${arm.id}` : ""}
              {arm.code === view.segment.armCode && (
                <>
                  {" "}
                  <Badge tone="info">
                    {t("automatic.campaign.arms.current")}
                  </Badge>
                </>
              )}
            </th>
            <td>
              <Progress
                value={arm.total ? arm.done : 0}
                max={Math.max(arm.total, 1)}
                label={`${arm.code} ${arm.done} / ${arm.total}`}
              />
            </td>
            <td className="ds-num">{arm.done}</td>
            <td className="ds-num">{arm.remaining}</td>
            <td className="ds-num">{arm.deviated}</td>
            <td className="ds-num">{arm.discarded}</td>
            {showCards && <td className="ds-num">{arm.unconfirmed}</td>}
          </tr>
        ))}
      </tbody>
    </Table>
  );
}

export function CampaignOverview({
  campaignId,
  api = wizardApi,
  intervalMs = 2000,
}: {
  campaignId: string;
  api?: OverviewApi;
  intervalMs?: number;
}) {
  const { t } = useLocale();
  const { value, error, loading, refresh } = usePolled(
    () => api.getCampaign(campaignId),
    intervalMs,
  );
  const keys = useRef(new IntentKeys());
  const [busy, setBusy] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  const run = async (action: "pause" | "resume" | "unblind") => {
    if (busy) return;
    const intent = `${action}:${campaignId}`;
    setBusy(action);
    setProblem(null);
    try {
      const result = await api.campaignCommand(
        campaignId,
        action,
        keys.current.idFor(intent),
      );
      if (result.result === "refused")
        setProblem(
          t(
            (result.code ? codeKey(result.code) : null) ??
              "automatic.campaign.refused",
          ),
        );
      keys.current.release(intent);
      refresh();
    } catch (e) {
      setProblem(failureText(e, t));
    } finally {
      setBusy(null);
    }
  };

  const attach = async () => {
    if (busy) return;
    const intent = `attach:${campaignId}`;
    setBusy("attach");
    setProblem(null);
    try {
      await api.attachCampaign(campaignId, keys.current.idFor(intent));
      keys.current.release(intent);
      refresh();
    } catch (e) {
      setProblem(failureText(e, t));
      keys.current.release(intent);
    } finally {
      setBusy(null);
    }
  };

  if (!value)
    return error ? (
      <RequestProblem
        action="automatic.campaign.load_failed"
        message={error}
        onRetry={refresh}
      />
    ) : (
      <p role="status">{loading ? t("Loading…") : ""}</p>
    );

  const view = viewOf(value);
  return (
    <>
      <header className="pg-head">
        <h1>{t("automatic.campaign.title")}</h1>
        <div className="pg-head-actions">
          {view.executionMode && (
            <Badge tone={view.executionMode === "dry_run" ? "warning" : "info"}>
              {t(
                view.executionMode === "dry_run"
                  ? "automatic.campaign.mode.dry_run"
                  : "automatic.campaign.mode.guided",
              )}
            </Badge>
          )}
          <Badge tone={STATE_TONE[view.state] ?? "neutral"}>
            {t(stateKey(view.state))}
          </Badge>
        </div>
      </header>
      <p className="pg-pool-muted">
        <code>{view.id}</code>
        {error && (
          <span className="pg-live-bad">
            {" · "}
            {t("automatic.campaign.stale")}
          </span>
        )}
      </p>

      {view.canAttach && (
        <Note tone="warning" role="alert">
          <strong>{t("automatic.campaign.controller.down")}</strong>{" "}
          {t("automatic.campaign.controller.down_body")}
          <div className="pg-row">
            <Button
              icon={Link2}
              loading={busy === "attach"}
              disabled={busy !== null}
              onClick={() => void attach()}
            >
              {t("automatic.campaign.controller.attach")}
            </Button>
          </div>
        </Note>
      )}

      {view.peeks > 0 && (
        <Note tone="warning" role="status">
          <strong>{t("automatic.campaign.peeked.title")}</strong>{" "}
          {t("automatic.campaign.peeked.body").replace(
            "{n}",
            String(view.peeks),
          )}
        </Note>
      )}

      {view.waitReason && view.todo?.kind !== "recover_run" && (
        <p className="pg-pool-hint">
          {t("automatic.campaign.wait_reason").replace(
            "{reason}",
            t(reasonKey(view.waitReason)),
          )}
        </p>
      )}

      {view.childRunId && view.executionMode === "dry_run" && (
        <p>
          <Link href={`/automatic/runs/${encodeURIComponent(view.childRunId)}`}>
            {t("automatic.campaign.child_run")}
          </Link>
        </p>
      )}

      {view.fused && (
        <Note tone="warning" role="alert">
          <strong>{t("automatic.campaign.fused.title")}</strong>{" "}
          {t("automatic.campaign.fused.body")}
        </Note>
      )}

      {view.blinded ? (
        <Note tone="info" role="status">
          <EyeOff size={14} aria-hidden="true" />{" "}
          {t("automatic.campaign.blind.note")}
        </Note>
      ) : (
        <Note tone="success" role="status">
          {t("automatic.campaign.blind.open")}
        </Note>
      )}

      {view.todo && (
        <CampaignTodoCard
          // A new challenge is a new question: start its checklist afresh.
          key={`${view.todo.kind}:${view.todo.challenge ?? ""}`}
          campaignId={campaignId}
          todo={view.todo}
          controllerDown={view.controllerDown}
          api={api}
          onDone={refresh}
        />
      )}

      {view.executionMode === "guided" && view.state !== "DRAFT" && (
        <CampaignCards
          campaignId={campaignId}
          knownCards={view.todo?.cards ?? []}
          unconfirmed={view.arms.reduce((n, a) => n + a.unconfirmed, 0)}
          api={api}
        />
      )}

      <section className="aw-panel" aria-labelledby="ac-progress">
        <h2 id="ac-progress">{t("automatic.campaign.progress.title")}</h2>
        <p>
          {t("automatic.campaign.progress.segment")
            .replace("{no}", String(view.segment.no))
            .replace("{total}", String(view.segment.total))
            .replace("{arm}", view.segment.armCode || "—")}
          {view.etaSeconds !== null && (
            <>
              {" · "}
              {t("automatic.campaign.progress.eta").replace(
                "{eta}",
                formatEta(view.etaSeconds),
              )}
            </>
          )}
        </p>
        <Progress
          value={view.done}
          max={Math.max(view.total, 1)}
          label={t("automatic.campaign.progress.total")
            .replace("{done}", String(view.done))
            .replace("{total}", String(view.total))}
        />
        <ArmsTable view={view} showCards={view.executionMode === "guided"} />
      </section>

      <section className="aw-panel" aria-labelledby="ac-safety">
        <h2 id="ac-safety">{t("automatic.campaign.safety.title")}</h2>
        <p>
          {t("automatic.campaign.safety.faults").replace(
            "{n}",
            String(view.faults),
          )}
          {" · "}
          <Badge tone={view.fused ? "danger" : "success"}>
            {view.fused
              ? t("automatic.campaign.safety.fused")
              : t("automatic.campaign.safety.ok")}
          </Badge>
        </p>
        <div className="pg-row">
          {view.paused ? (
            <Button
              icon={Play}
              loading={busy === "resume"}
              disabled={!view.canResume || view.controllerDown || busy !== null}
              onClick={() => void run("resume")}
            >
              {t("automatic.campaign.resume")}
            </Button>
          ) : (
            <Button
              icon={Pause}
              loading={busy === "pause"}
              disabled={!view.canPause || view.controllerDown || busy !== null}
              onClick={() => void run("pause")}
            >
              {t("automatic.campaign.pause")}
            </Button>
          )}
          {view.controllerDown && !view.finished && (
            <span className="pg-pool-hint">
              {t("automatic.campaign.controller.needed")}
            </span>
          )}
          {!view.controllerDown &&
            !view.paused &&
            !view.canPause &&
            !view.finished && (
              <span className="pg-pool-hint">
                {t("automatic.campaign.pause_later")}
              </span>
            )}
          {view.reportReady && (
            <Link
              className="ds-btn ds-btn--primary ds-btn--md"
              href={`/automatic/campaigns/${encodeURIComponent(view.id)}/report`}
            >
              {t("automatic.campaign.open_report")}
            </Link>
          )}
        </div>
        {problem && (
          <RequestProblem
            action="automatic.campaign.command_failed"
            message={problem}
          />
        )}
      </section>

      {view.blinded && (
        <details className="aw-panel ac-unblind">
          <summary>{t("automatic.campaign.unblind.summary")}</summary>
          <Note tone="warning">{t("automatic.campaign.unblind.warning")}</Note>
          <PhraseConfirm
            phrase="unblind"
            label={t("automatic.campaign.unblind.type")}
            buttonLabel={t("automatic.campaign.unblind.button")}
            variant="danger"
            busy={busy === "unblind"}
            onConfirm={() => void run("unblind")}
          />
        </details>
      )}
    </>
  );
}
