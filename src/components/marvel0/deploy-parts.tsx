"use client";
// The pieces of the Policy server card that only show: the chosen model, the
// state with what the log has said, the checks before a deploy and the unit's
// log. Nothing here sends a request; the card (deploy-panel.tsx) owns every
// press.
import type { ReactNode } from "react";
import { CircleCheck, TriangleAlert } from "lucide-react";
import { Badge } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { ago } from "@/components/pool/pool-progress";
import { WordedText } from "./action-parts";
import { warningSay, type ModelRow } from "./catalog-logic";
import {
  checkRows,
  failureSay,
  liveSay,
  reasonSay,
  readinessRows,
  shownDeploy,
  stateSay,
} from "./deploy-logic";
import type { Say } from "./marvel0-logic";
import { sayText } from "./marvel0-say";
import type { DeployPoll } from "./use-marvel0-actions";

const ACTION_KEY: Record<string, string> = {
  deploy: "marvel0.deploy.action.deploy",
  stop: "marvel0.deploy.action.stop",
  other: "marvel0.deploy.action.other",
};
const LEVEL_KEY = {
  ok: "marvel0.deploy.level.ok",
  refuse: "marvel0.deploy.level.refuse",
  unknown: "marvel0.deploy.level.unknown",
} as const;
const LEVEL_TONE = {
  ok: "success",
  refuse: "danger",
  unknown: "neutral",
} as const;

/** What the person is deploying: the badges and facts of the chosen model. */
export function ModelFacts({ model }: { model: ModelRow }) {
  const { t } = useLocale();
  return (
    <div className="rg-model" data-testid="marvel0-deploy-model">
      <div className="rg-model__badges">
        {model.verified ? (
          <Badge tone="success" icon={CircleCheck}>
            {t("marvel0.deploy.model.verified")}
          </Badge>
        ) : (
          <Badge tone="warning" icon={TriangleAlert}>
            {t("marvel0.deploy.model.unverified")}
          </Badge>
        )}
        <Badge tone="neutral" icon={null}>
          {t("marvel0.deploy.model.role")}
        </Badge>
        {model.step !== null && (
          <Badge tone="neutral" icon={null}>
            {sayText(
              {
                key: "marvel0.deploy.model.step",
                params: { step: model.step },
              },
              t,
            )}
          </Badge>
        )}
      </div>
      {!model.verified && (
        <p className="rg-muted">{t("marvel0.deploy.model.unverified_hint")}</p>
      )}
      {model.restrictedPrompts && (
        <p className="rg-muted">{t("marvel0.deploy.model.restricted")}</p>
      )}
      {model.cfgCapable && (
        <p className="rg-muted">{t("marvel0.deploy.model.cfg")}</p>
      )}
      {model.warnings.length > 0 && (
        <ul className="rg-notes">
          {model.warnings.map((code) => (
            <li key={code}>
              <WordedText worded={warningSay(code)} />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function StatusBlock({
  status,
  current,
}: {
  status: NonNullable<DeployPoll["status"]>;
  current: boolean;
}) {
  const { t } = useLocale();
  const reason = reasonSay(status);
  const failure = failureSay(status);
  const ready = readinessRows(status);
  const live = liveSay(status);
  const deploy = shownDeploy(status);
  const facts: Array<[string, ReactNode]> = [];
  if (deploy) {
    facts.push(
      [
        t("marvel0.deploy.fact.model"),
        <code key="m">{deploy.modelId ?? "?"}</code>,
      ],
      [
        t("marvel0.deploy.fact.config"),
        <code key="c">{deploy.config ?? "?"}</code>,
      ],
      [
        t("marvel0.deploy.fact.rtc"),
        deploy.rtc === null
          ? t("marvel0.value.unknown")
          : t(
              deploy.rtc
                ? "marvel0.deploy.value.on"
                : "marvel0.deploy.value.off",
            ),
      ],
      [
        t("marvel0.deploy.fact.fraction"),
        deploy.fraction === null ? "?" : deploy.fraction.toFixed(2),
      ],
    );
    if (deploy.cfgScale !== null)
      facts.push([t("marvel0.deploy.fact.scale"), String(deploy.cfgScale)]);
    if (deploy.ageS !== null)
      facts.push([t("marvel0.deploy.fact.started"), ago(deploy.ageS, t)]);
    facts.push([
      t("marvel0.deploy.fact.unit"),
      <span key="u">
        <code lang="en">{deploy.unit ?? "?"}</code>
        {" · "}
        {status.unit.state === null ? (
          t("marvel0.value.unknown")
        ) : (
          <span lang="en">
            {status.unit.state}
            {status.unit.sub ? ` (${status.unit.sub})` : ""}
          </span>
        )}
      </span>,
    ]);
  }
  facts.push([
    sayText(
      {
        key: "marvel0.fact.port",
        params: { port: status.listener.port ?? "?" },
      },
      t,
    ),
    status.listener.present === null
      ? t("marvel0.value.unknown")
      : t(
          status.listener.present
            ? "marvel0.port.listening"
            : "marvel0.port.closed",
        ),
  ]);
  if (status.listener.established !== null && status.listener.established > 0)
    facts.push([
      t("marvel0.deploy.fact.clients"),
      String(status.listener.established),
    ]);
  facts.push([
    t("marvel0.deploy.fact.gpu"),
    status.gpu.ok && status.gpu.freeMib !== null
      ? sayText(
          {
            key: "marvel0.deploy.gpu.line",
            params: {
              used: status.gpu.usedMib ?? "?",
              total: status.gpu.totalMib ?? "?",
              free: status.gpu.freeMib,
            },
          },
          t,
        )
      : t("marvel0.deploy.gpu.unknown"),
  ]);
  return (
    <div className="rg-status" aria-busy={!current || undefined}>
      <p data-testid="marvel0-deploy-summary">{sayText(stateSay(status), t)}</p>
      {failure && (
        <p className="rg-bad" data-testid="marvel0-deploy-why">
          <WordedText worded={failure} />
        </p>
      )}
      {reason && (
        <p className="rg-muted">
          <WordedText worded={reason} />
        </p>
      )}
      <dl className="rg-facts" data-testid="marvel0-deploy-facts">
        {facts.map(([label, value], index) => (
          <div key={index} className="rg-fact">
            <dt>{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
      {ready.length > 0 && (
        <>
          <strong>{t("marvel0.deploy.ready.title")}</strong>
          <ul className="rg-notes" data-testid="marvel0-deploy-ready">
            {ready.map((row) => (
              <li key={row.id}>
                {t(row.label)}:{" "}
                {row.value === null
                  ? t("marvel0.deploy.ready.not_read")
                  : row.value
                    ? t("marvel0.value.yes")
                    : t("marvel0.deploy.ready.not_yet")}
              </li>
            ))}
          </ul>
        </>
      )}
      {live && (
        <p className="rg-muted" data-testid="marvel0-deploy-live">
          {sayText(live, t)}
        </p>
      )}
      {status.operation && (
        <p role="status">
          {sayText(
            {
              key: "marvel0.deploy.operation",
              params: {
                action: {
                  key: ACTION_KEY[status.operation.action] ?? ACTION_KEY.other,
                },
              },
            },
            t,
          )}
        </p>
      )}
    </div>
  );
}

export function ChecksBlock({
  status,
}: {
  status: NonNullable<DeployPoll["status"]>;
}) {
  const { t } = useLocale();
  const rows = checkRows(status);
  if (rows.length === 0) return null;
  const bad = rows.filter((row) => row.level !== "ok").length;
  const summary: Say =
    bad === 0
      ? { key: "marvel0.deploy.checks.all_ok", params: { n: rows.length } }
      : { key: "marvel0.deploy.checks.blocked", params: { n: bad } };
  // Open when something blocks a deploy that the person may want. A server of
  // this page that already runs blocks a second one by itself (its port, its
  // unit): that is not news, so the list stays folded then.
  const running =
    status.state === "ready" ||
    status.state === "starting" ||
    status.state === "stopping";
  return (
    <details className="rg-details" open={(bad > 0 && !running) || undefined}>
      <summary>
        {t("marvel0.deploy.checks.title")} · {sayText(summary, t)}
      </summary>
      <ul className="rg-checks" data-testid="marvel0-deploy-checks">
        {rows.map((row) => (
          <li key={row.code} data-level={row.level}>
            <Badge tone={LEVEL_TONE[row.level]}>
              {t(LEVEL_KEY[row.level])}
            </Badge>{" "}
            <strong>
              {row.title ? t(row.title) : <code lang="en">{row.code}</code>}
            </strong>
            {": "}
            <WordedText worded={row.say} />
          </li>
        ))}
      </ul>
    </details>
  );
}

export function LogBlock({
  poll,
  open,
  onOpen,
}: {
  poll: DeployPoll;
  /** Open from the start: a deploy that failed or ended is read from its log. */
  open: boolean;
  onOpen: (open: boolean) => void;
}) {
  const { t } = useLocale();
  return (
    <details
      className="rg-details"
      data-testid="marvel0-deploy-log-details"
      open={open || undefined}
      onToggle={(event) => onOpen(event.currentTarget.open)}
    >
      <summary>{t("marvel0.deploy.log.title")}</summary>
      <p className="rg-muted">{t("marvel0.deploy.log.hint")}</p>
      {poll.logProblem && (
        <p className="rg-bad" role="status">
          {t("marvel0.deploy.log.unreadable")}
        </p>
      )}
      {poll.log && poll.log.lines.length > 0 ? (
        <pre lang="en" className="rg-log" data-testid="marvel0-deploy-log">
          {poll.log.lines.join("\n")}
        </pre>
      ) : (
        !poll.logProblem && (
          <p className="rg-muted">{t("marvel0.deploy.log.empty")}</p>
        )
      )}
    </details>
  );
}
