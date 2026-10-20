"use client";
// The pieces of the marvel0: a component's card, the "robot idle?"
// banner and the line saying how fresh the answers are. Thin on purpose:
// what is shown is decided in marvel0-logic.ts. Nothing here sends a request,
// starts or stops anything.
import { useEffect, useState } from "react";
import {
  CircleCheck,
  CircleDashed,
  CircleHelp,
  CircleX,
  Clock,
  Eye,
  RefreshCw,
  type LucideIcon,
} from "lucide-react";
import { Badge, Button, Card } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { ago } from "@/components/pool/pool-progress";
import {
  STATE_KEY,
  STATE_TONE,
  type IdleState,
  type Marvel0Banner,
  type Marvel0Card,
  type Marvel0State,
} from "./marvel0-logic";
import { sayText } from "./marvel0-say";

const STATE_ICON: Record<Marvel0State, LucideIcon> = {
  ok: CircleCheck,
  red: CircleX,
  stale: Clock,
  not_started: CircleDashed,
  unknown: CircleHelp,
};

// "Idle" only says nothing was seen, so it has no success colour.
const IDLE_TONE: Record<IdleState, "warning" | "neutral"> = {
  idle: "neutral",
  busy: "warning",
  unknown: "neutral",
};
const IDLE_ICON: Record<IdleState, LucideIcon> = {
  idle: Eye,
  busy: Clock,
  unknown: CircleHelp,
};
const IDLE_KEY: Record<IdleState, string> = {
  idle: "marvel0.idle.idle",
  busy: "marvel0.idle.busy",
  unknown: "marvel0.idle.unknown",
};

export function Marvel0CardView({ card }: { card: Marvel0Card }) {
  const { t } = useLocale();
  return (
    <Card
      padding="compact"
      className={`rg-card rg-card--${card.state}`}
      data-testid={`marvel0-card-${card.id}`}
      data-state={card.state}
      title={t(card.title)}
      description={sayText(card.summary, t)}
      actions={
        <Badge tone={STATE_TONE[card.state]} icon={STATE_ICON[card.state]}>
          {t(STATE_KEY[card.state])}
        </Badge>
      }
    >
      {card.facts.length > 0 && (
        <dl className="rg-facts">
          {card.facts.map((fact, index) => (
            <div key={index} className="rg-fact">
              <dt>{sayText(fact.label, t)}</dt>
              <dd>{sayText(fact.value, t)}</dd>
            </div>
          ))}
        </dl>
      )}
      {card.notes.length > 0 && (
        <ul className="rg-notes">
          {card.notes.map((note, index) =>
            typeof note === "string" ? (
              <li key={index} lang="en">
                {note}
              </li>
            ) : (
              <li key={index}>{sayText(note, t)}</li>
            ),
          )}
        </ul>
      )}
      <p className="rg-hint">{t(card.hint)}</p>
    </Card>
  );
}

export function IdleBannerView({ banner }: { banner: Marvel0Banner }) {
  const { t } = useLocale();
  return (
    <section
      className={`rg-banner rg-banner--${banner.state}`}
      aria-labelledby="marvel0-idle-title"
      data-testid="marvel0-idle"
      data-state={banner.state}
    >
      <div className="rg-banner__head">
        <h2 id="marvel0-idle-title">{t("marvel0.idle.title")}</h2>
        <Badge tone={IDLE_TONE[banner.state]} icon={IDLE_ICON[banner.state]}>
          {t(IDLE_KEY[banner.state])}
        </Badge>
      </div>
      {banner.note && <p role="status">{sayText(banner.note, t)}</p>}
      {banner.reasons.length > 0 && (
        <ul className="rg-banner__reasons">
          {banner.reasons.map((reason, index) => (
            <li key={index}>
              <span className="rg-source">{sayText(reason.source, t)}</span>
              {" · "}
              {sayText(reason.state, t)}
              {reason.say ? (
                <span className="rg-what">{sayText(reason.say, t)}</span>
              ) : (
                <span lang="en" className="rg-server">
                  {reason.text}
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
      {banner.hint && (
        <p className="rg-banner__hint" data-testid="marvel0-idle-hint">
          {sayText(banner.hint, t)}
        </p>
      )}
      <p className="rg-muted">{t("marvel0.idle.advisory")}</p>
    </section>
  );
}

/** "Updated 3 s ago · refreshes every 3 s", counting up between polls. Before
 * the first answer the page body says "Loading…"; this line does not. */
export function Freshness({
  loaded,
  lastOk,
  delay,
  failures,
  errors,
  onRefresh,
}: {
  /** The first round of requests has finished (either way). */
  loaded: boolean;
  lastOk: number | null;
  delay: number;
  failures: number;
  /** The failure texts of the requests that failed in the last round. */
  errors: string[];
  onRefresh: () => void;
}) {
  const { t } = useLocale();
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => {
      if (document.visibilityState !== "hidden") setNow(Date.now());
    }, 1000);
    return () => clearInterval(timer);
  }, []);
  const answer =
    lastOk != null
      ? `${t("Updated")} ${ago((now - lastOk) / 1000, t)}`
      : loaded
        ? t("marvel0.fresh.none")
        : null;
  return (
    <p className="rg-fresh" aria-live="off">
      <span>
        {answer !== null && `${answer} · `}
        {t("refreshes every {n} s").replace(
          "{n}",
          String(Math.round(delay / 1000)),
        )}
        {" · "}
        {t("marvel0.fresh.paused")}
      </span>
      {errors.length > 0 && (
        <span className="rg-bad" role="status">
          {t("last request failed")} ({errors.join("; ")})
          {failures > 0 && `, ${t("retrying")}`}
        </span>
      )}
      <Button size="sm" icon={RefreshCw} onClick={onRefresh}>
        {t("Refresh")}
      </Button>
    </p>
  );
}
