"use client";
// The live evaluation entry of the navigation and of the home page. Both
// appear where a live annotation service is shown: the product LEVI once it
// finds the live workspace (levi/live/locate.py), or the live workspace's own
// page (`levi live start --ui`). Without one they render nothing (and cost a
// status request a minute, see live-pulse-store).
import Link from "next/link";
import { CircleDot } from "lucide-react";
import { Icon, TONE_ICON, Tooltip, type Tone } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { PULSE_NOTES, type Pulse, type PulseLight } from "./live-logic";
import { useLivePulse } from "./use-live-pulse";

/** Each light as a status tone: shape and colour, never colour alone. */
export const PULSE_TONE: Record<PulseLight, Tone> = {
  green: "success",
  blue: "info",
  amber: "warning",
  red: "danger",
  grey: "neutral",
};

/** A word for each light (read by screen readers next to the shape). */
export const PULSE_WORD: Record<PulseLight, string> = {
  green: "Running normally",
  blue: "Labelling",
  amber: "Needs attention",
  red: "Fault",
  grey: "Status unknown",
};

/** The live service's state as a ds status shape in its colour (check,
 * dot that breathes while labelling, triangle, cross, circle) with the
 * state in words for screen readers. */
export function PulseDot({ pulse }: { pulse: Pulse }) {
  const { t } = useLocale();
  const tone = PULSE_TONE[pulse.light];
  const labelling = pulse.light === "blue";
  return (
    <span
      className={`levi-pulse-dot is-${pulse.light} ds-status--${tone}`}
      data-light={pulse.light}
      data-tone={tone}
    >
      <Icon
        icon={labelling ? CircleDot : TONE_ICON[tone]}
        className={labelling ? "ds-breathe" : undefined}
      />
      <span className="ds-sr-only">{t(PULSE_WORD[pulse.light])}</span>
    </span>
  );
}

/** The sentence a hover (and a screen reader) gets. */
export function usePulseNote(pulse: Pulse): string {
  const { t } = useLocale();
  return t(PULSE_NOTES[pulse.reason]);
}

/** The navigation entry. `current` marks it as the page shown
 * (`aria-current`); the state sentence is its tooltip and part of its name. */
export function LiveNavLink({ current = false }: { current?: boolean }) {
  const { t } = useLocale();
  const { enabled, pulse } = useLivePulse();
  const note = usePulseNote(pulse);
  if (!enabled) return null;
  const label = t("Live evaluation");
  const count = pulse.count > 0 ? String(Math.min(pulse.count, 99)) : "";
  return (
    <Tooltip content={`${label}: ${note}`} describe={false} placement="bottom">
      <Link
        href="/live"
        className={`levi-live-nav is-${pulse.light}`}
        aria-current={current ? "page" : undefined}
        aria-label={`${label}${count ? ` (${count})` : ""}: ${note}`}
      >
        <PulseDot pulse={pulse} />
        <span>{label}</span>
        {count && <span className="levi-pulse-count">{count}</span>}
      </Link>
    </Tooltip>
  );
}

/** A large link at the top of the home page while a live service is shown. */
export function LiveBanner() {
  const { t } = useLocale();
  const { enabled, pulse } = useLivePulse();
  const note = usePulseNote(pulse);
  if (!enabled) return null;
  return (
    <Link
      href="/live"
      className={`levi-live-home is-${pulse.light}`}
      aria-label={`${t("Live evaluation & annotation")}: ${note}`}
    >
      <PulseDot pulse={pulse} />
      <span className="levi-live-home-text">
        <strong>{t("Live evaluation & annotation")}</strong>
        <span>{note}</span>
      </span>
      {pulse.count > 0 && (
        <span className="levi-pulse-count">{Math.min(pulse.count, 99)}</span>
      )}
      <span className="levi-live-home-go">{t("Open the live page")} →</span>
    </Link>
  );
}
