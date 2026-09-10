"use client";
import Link from "next/link";
import { LanguageSwitch, T } from "./levi-locale";
export default function LeviHeader() {
  return (
    <T>
      {
        <header className="levi-header">
          <Link href="/" className="levi-wordmark">
            <span className="levi-mark">L↗</span>LEVI
            <span className="levi-wordmark-caption">ROBOT DATA ATELIER</span>
          </Link>
          <nav>
            <Link href="/explore">
              <T>Explore</T>
            </Link>
            <Link href="/workbench">
              <T>Conversion & review</T>
            </Link>
            <Link href="/guide">
              <T>Guide</T>
            </Link>
            <LanguageSwitch />
          </nav>
        </header>
      }
    </T>
  );
}
