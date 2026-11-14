"use client";
/**
 * The "G …" chip: shown while a go-to chord waits for its second key, so a
 * person who pressed G by accident (or is unsure) sees that the next letter
 * will move them to another page. The frame hides it when the letter comes,
 * another key is pressed, or the chord window (CHORD_WINDOW_MS) ends.
 */
import { Kbd } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";

export function ChordChip({ pending }: { pending: boolean }) {
  const { t } = useLocale();
  if (!pending) return null;
  return (
    <div className="levi-chord-chip" role="status">
      <Kbd>G</Kbd>
      <span>{t("then a page letter")} …</span>
    </div>
  );
}
