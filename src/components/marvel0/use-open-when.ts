"use client";
// A fold that opens by itself when something goes wrong and is then the
// reader's to close: `open={red}` would collapse it under the person reading
// it the moment a poll says the card is no longer red.
import { useEffect, useRef } from "react";

/** The ref of a <details> that opens when `when` turns true (and at mount if
 * it is already true). It never closes the fold. */
export function useOpenWhen<T extends HTMLDetailsElement>(when: boolean) {
  const ref = useRef<T>(null);
  useEffect(() => {
    if (when && ref.current && !ref.current.open) ref.current.open = true;
  }, [when]);
  return ref;
}
