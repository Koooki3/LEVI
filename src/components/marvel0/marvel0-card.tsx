"use client";
// The pieces of the marvel0: a component's card, a step's heading, the "robot
// idle?" banner and the line saying how fresh the answers are. Thin on
// purpose: what is shown is decided in marvel0-logic.ts. Nothing here sends a
// request, starts or stops anything; the chain at the top is chain-strip.tsx.
import { useEffect, useState, type ReactNode } from "react";
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
  type Marvel0Fact,
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

export function Marvel0CardView({
  card,
  controls,
  badge,
  domId,
  edge,
}: {
  card: Marvel0Card;
  /** The card's own controls (buttons, progress), shown under the summary. */
  controls?: ReactNode;
  /** The badge in the header when the card's controls know the state better
   * than the passive probe (the probe's state stays in `data-state` and in
   * the facts). */
  badge?: ReactNode;
  /** An id the chain at the top of the page moves to. */
  domId?: string;
  /** The state the coloured edge shows, when the controls know it better than
   * the probe (the probe's state stays in `data-state`). With `edge` the
   * probe's one-line summary becomes the first fact, so that two sources do
   * not each claim the headline. */
  edge?: Marvel0State | "working";
}) {
  const { t } = useLocale();
  const facts: Marvel0Fact[] = edge
    ? [
        { label: { key: "marvel0.ctl.probe" }, value: card.summary },
        ...card.facts,
      ]
    : card.facts;
  return (
    <Card
      padding="compact"
      className={`rg-card rg-card--${edge ?? card.state}`}
      id={domId}
      tabIndex={domId ? -1 : undefined}
      data-testid={`marvel0-card-${card.id}`}
      data-state={card.state}
      title={t(card.title)}
      description={edge ? undefined : sayText(card.summary, t)}
      actions={
        badge ?? (
          <Badge tone={STATE_TONE[card.state]} icon={STATE_ICON[card.state]}>
            {t(STATE_KEY[card.state])}
          </Badge>
        )
      }
    >
      {controls}
      {facts.length > 0 ? (
        // The readings, and the line saying where they are read from, are one
        // fold under the card's state and buttons: the probes' numbers are one
        // click away (and open when the card is red).
        <details
          className="rg-details rg-card__probe"
          open={card.state === "red" ? true : undefined}
        >
          <summary>
            {t("marvel0.card.facts").replace("{n}", String(facts.length))}
          </summary>
          <Facts facts={facts} />
          <p className="rg-hint">{t(card.hint)}</p>
        </details>
      ) : (
        <p className="rg-hint">{t(card.hint)}</p>
      )}
      {card.notes.length > 0 && (
        // The reasons and what the servers wrote: folded away unless the
        // card is red, so a row of cards stays short.
        <details
          className="rg-details rg-card__more"
          open={card.state === "red" ? true : undefined}
        >
          <summary>
            {t("marvel0.card.details").replace(
              "{n}",
              String(card.notes.length),
            )}
          </summary>
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
        </details>
      )}
    </Card>
  );
}

function Facts({ facts }: { facts: Marvel0Fact[] }) {
  const { t } = useLocale();
  if (facts.length === 0) return null;
  return (
    <dl className="rg-facts">
      {facts.map((fact, index) => (
        <div key={index} className="rg-fact">
          <dt>{sayText(fact.label, t)}</dt>
          <dd>{sayText(fact.value, t)}</dd>
        </div>
      ))}
    </dl>
  );
}

/** The heading of one of the page's numbered steps: "Step 2" and its name. The
 * word keeps the steps apart from the numbers of the chain's nodes. */
export function StepHeading({
  n,
  id,
  children,
}: {
  n: number;
  id: string;
  children: ReactNode;
}) {
  const { t } = useLocale();
  return (
    <h2 id={id} className="rg-step__title">
      <span className="rg-step__tag">
        {t("marvel0.step.n").replace("{n}", String(n))}
      </span>
      <span className="rg-step__name">{children}</span>
    </h2>
  );
}

/** The "robot idle?" guess. It is advisory, so it is one line (the title and
 * the guess) that opens to the reasons; it opens by itself only when the
 * machine looks busy. */
export function IdleBannerView({ banner }: { banner: Marvel0Banner }) {
  const { t } = useLocale();
  return (
    <details
      className={`rg-banner rg-banner--${banner.state}`}
      data-testid="marvel0-idle"
      data-state={banner.state}
      open={banner.state === "busy" ? true : undefined}
    >
      <summary className="rg-banner__head">
        <h2 id="marvel0-idle-title">{t("marvel0.idle.title")}</h2>
        <Badge tone={IDLE_TONE[banner.state]} icon={IDLE_ICON[banner.state]}>
          {t(IDLE_KEY[banner.state])}
        </Badge>
      </summary>
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
    </details>
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
      <span
        className={`rg-fresh__dot${errors.length > 0 || lastOk == null ? " rg-fresh__dot--late" : " ds-breathe"}`}
        aria-hidden="true"
      />
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
