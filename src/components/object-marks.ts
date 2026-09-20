"use client";
import { useSyncExternalStore } from "react";

/** Timestamps that carry an object annotation, shared with the playback bar.
 *
 * Objects are annotated on sampled frames, so scrubbing between them shows
 * nothing new. The bar marks the frames that were annotated and can jump
 * between them; the player owns the data, so it publishes it here rather than
 * threading it through every component in between.
 */
let marks: number[] = [];
const listeners = new Set<() => void>();

function same(a: number[], b: number[]): boolean {
  return a.length === b.length && a.every((value, index) => value === b[index]);
}

export function setObjectMarks(next: number[]): void {
  const sorted = [...new Set(next)].sort((a, b) => a - b);
  if (same(marks, sorted)) return;
  marks = sorted;
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useObjectMarks(): number[] {
  return useSyncExternalStore(
    subscribe,
    () => marks,
    () => marks,
  );
}

/** The next annotated instant strictly after (or before) `from`. */
export function nextMark(from: number, direction: 1 | -1): number | null {
  const ordered = direction === 1 ? marks : [...marks].reverse();
  const found = ordered.find((value) =>
    direction === 1 ? value > from + 0.05 : value < from - 0.05,
  );
  return found ?? null;
}
