"use client";
import {
  createContext,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
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

/**
 * Publishes the drawer's height as `--vw-inspector-h` on the layout around
 * it, so on narrow windows the content and the episode list keep that much
 * room at the bottom and nothing ends up under the drawer.
 */
export function syncInspectorHeight(aside: HTMLElement | null): void {
  const host = aside?.parentElement;
  if (!aside || !host) return;
  host.style.setProperty(
    "--vw-inspector-h",
    `${Math.ceil(aside.getBoundingClientRect().height)}px`,
  );
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
  const asideRef = useRef<HTMLElement | null>(null);
  useEffect(() => {
    const aside = asideRef.current;
    if (!enabled || !aside) return;
    syncInspectorHeight(aside);
    if (typeof ResizeObserver !== "function") return;
    const observer = new ResizeObserver(() => syncInspectorHeight(aside));
    observer.observe(aside);
    return () => {
      observer.disconnect();
      aside.parentElement?.style.removeProperty("--vw-inspector-h");
    };
  }, [enabled, open]);
  return (
    <InspectorSlot.Provider value={enabled ? slot : null}>
      {children}
      {enabled && (
        <aside
          ref={asideRef}
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
