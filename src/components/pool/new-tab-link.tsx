"use client";
import type { ReactNode } from "react";
import Link from "next/link";
import { ArrowUpRight } from "lucide-react";
import { Icon } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";

/** A link to the episode viewer. It opens in a new tab so the composition,
 * filters and forms on this page are still there when the person comes back;
 * the arrow is the visible cue and carries the words for a screen reader. */
export function NewTabLink({
  href,
  children,
}: {
  href: string;
  children: ReactNode;
}) {
  const { t } = useLocale();
  return (
    <Link href={href} target="_blank" rel="noreferrer">
      {children}
      <Icon icon={ArrowUpRight} label={t("opens in a new tab")} />
    </Link>
  );
}
