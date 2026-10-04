"use client";
import Link from "next/link";
import { LanguageSwitch, T } from "./levi-locale";
import { offersTrainingPool } from "./live/embedding";
import { LiveNavLink } from "./live/live-nav";
import { useLivePulse } from "./live/use-live-pulse";
export default function LeviHeader() {
  // The live workspace's own LEVI (`levi live start --ui`) offers no training
  // pool: the pool is the product LEVI's (docs/LIVE.md).
  const { enabled, embedded } = useLivePulse();
  return (
    <T>
      {
        <header className="levi-header">
          <Link href="/" className="levi-wordmark">
            <span className="levi-mark">L↗</span>LEVI
            <span className="levi-wordmark-caption">ROBOT DATA ATELIER</span>
          </Link>
          <nav>
            <LiveNavLink />
            <Link href="/explore">
              <T>Explore</T>
            </Link>
            <Link href="/workbench">
              <T>Conversion & review</T>
            </Link>
            {offersTrainingPool(enabled, embedded) && (
              <Link href="/pool">
                <T>Training pool</T>
              </Link>
            )}
            <Link href="/guide">
              <T>Guide</T>
            </Link>
            <Link href="/report">
              <T>Report</T>
            </Link>
            <button
              className="levi-language"
              onClick={() =>
                window.dispatchEvent(new Event("levi-agent-toggle"))
              }
            >
              <T>Agent Workbench</T>
            </button>
            <button
              className="levi-language"
              onClick={() =>
                window.dispatchEvent(new CustomEvent("levi-agent-connections"))
              }
            >
              <T>Accounts & connections</T>
            </button>
            <LanguageSwitch />
          </nav>
        </header>
      }
    </T>
  );
}
