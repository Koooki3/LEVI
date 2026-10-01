"use client";
// The live evaluation entry of the navigation and of the home page. Both
// exist only in the live annotation workspace; anywhere else they render
// nothing (and cost one status request per page load, see live-pulse-store).
import Link from "next/link";
import { useLocale } from "@/components/levi-locale";
import { PULSE_NOTES, type Pulse } from "./live-logic";
import { useLivePulse } from "./use-live-pulse";

export function PulseDot({ pulse }: { pulse: Pulse }) {
  return (
    <span
      className={`levi-pulse-dot is-${pulse.light}`}
      data-light={pulse.light}
      aria-hidden="true"
    />
  );
}

/** The sentence a hover (and a screen reader) gets. */
export function usePulseNote(pulse: Pulse): string {
  const { t } = useLocale();
  return t(PULSE_NOTES[pulse.reason]);
}

export function LiveNavLink() {
  const { t } = useLocale();
  const { enabled, pulse } = useLivePulse();
  const note = usePulseNote(pulse);
  if (!enabled) return null;
  const label = t("Live evaluation");
  const count = pulse.count > 0 ? String(Math.min(pulse.count, 99)) : "";
  return (
    <Link
      href="/live"
      className={`levi-live-nav is-${pulse.light}`}
      title={`${label}: ${note}`}
      aria-label={`${label}${count ? ` (${count})` : ""}: ${note}`}
    >
      <PulseDot pulse={pulse} />
      <span>{label}</span>
      {count && <span className="levi-pulse-count">{count}</span>}
    </Link>
  );
}

/** A large link at the top of the home page of the live workspace. */
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
