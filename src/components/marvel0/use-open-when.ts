"use client";
// A fold that opens by itself when something goes wrong and is then the
// reader's to close: `open={red}` would collapse it under the person reading
// it the moment a poll says the card is no longer red.
import { useCallback, useEffect, useRef } from "react";

/** A callback ref for a <details> that opens when `when` turns true, at mount
 * if it is already true, and when the fold itself appears while `when` is
 * true (reasons that arrive on a later poll of a card that is already red).
 * It never closes the fold. */
export function useOpenWhen<T extends HTMLDetailsElement>(when: boolean) {
  const node = useRef<T | null>(null);
  const wanted = useRef(when);
  wanted.current = when;
  const ref = useCallback((el: T | null) => {
    node.current = el;
    if (el && wanted.current && !el.open) el.open = true;
  }, []);
  useEffect(() => {
    if (when && node.current && !node.current.open) node.current.open = true;
  }, [when]);
  return ref;
}
