"use client";
// The live evaluation entry of the navigation. Always offered: where a live
// annotation service is shown (the product LEVI once it finds the live
// workspace, levi/live/locate.py, or the live workspace's own page,
// `levi live start --ui`) it carries the service's state and the count of
// things that need a person; without one it is a plain link to the page that
// says how to start one (the status request it costs, once a minute, is in
// live-pulse-store). The home page has its own live card (src/components/home).
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
  const label = t("Live evaluation");
  // No live service (or not known yet): a plain entry like the others, so
  // the page that explains how to start one can be found.
  if (!enabled)
    return (
      <Link
        href="/live"
        className="levi-shell-link ds-focus"
        aria-current={current ? "page" : undefined}
      >
        {label}
      </Link>
    );
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
