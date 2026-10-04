"use client";
import { createContext, useContext, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { PanelRightClose, PanelRightOpen } from "lucide-react";
import { IconButton } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import "./viewer.css";

/**
 * The episode viewer's inspector (proposal §8.3): a right column that shows
 * the selected time segment's or object's properties and edit form. The
 * panels keep rendering their own forms (same component, same state, same
 * handlers); `InspectorPortal` only moves the form's DOM into the column.
 * Without a column (other pages, tests) the form renders where it is.
 */
const InspectorSlot = createContext<HTMLElement | null>(null);

/** The column's body element, or null when there is no inspector column. */
export function useInspectorSlot(): HTMLElement | null {
  return useContext(InspectorSlot);
}

/** Renders `children` in the inspector column if there is one, else here. */
export function InspectorPortal({ children }: { children: ReactNode }) {
  const slot = useInspectorSlot();
  return slot ? createPortal(children, slot) : <>{children}</>;
}

/**
 * Provides the inspector slot to `children` and draws the column beside
 * them when `enabled`. Wide windows: a 320 px column that can be collapsed
 * to a thin rail. Below 1200 px: a drawer along the bottom edge, collapsed
 * to its title bar until opened.
 */
export function InspectorLayout({
  enabled,
  children,
}: {
  enabled: boolean;
  children: ReactNode;
}) {
  const { t } = useLocale();
  const [slot, setSlot] = useState<HTMLElement | null>(null);
  // Open beside the content on wide windows; on narrow ones the drawer
  // starts collapsed so it does not cover the video.
  const [open, setOpen] = useState(
    () =>
      typeof window === "undefined" ||
      typeof window.matchMedia !== "function" ||
      window.matchMedia("(min-width: 1200px)").matches,
  );
  return (
    <InspectorSlot.Provider value={enabled ? slot : null}>
      {children}
      {enabled && (
        <aside
          className="vw-inspector annotations-skin"
          data-open={open ? "true" : "false"}
          aria-label={t("Inspector")}
        >
          <div className="vw-inspector-head">
            <h2 className="vw-label">{t("Inspector")}</h2>
            <IconButton
              icon={open ? PanelRightClose : PanelRightOpen}
              size="sm"
              label={t(open ? "Collapse inspector" : "Expand inspector")}
              aria-expanded={open}
              tooltipPlacement="bottom"
              onClick={() => setOpen((value) => !value)}
            />
          </div>
          <div className="vw-inspector-body" ref={setSlot} hidden={!open} />
        </aside>
      )}
    </InspectorSlot.Provider>
  );
}
