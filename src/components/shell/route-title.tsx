"use client";
/**
 * The browser tab's title: the page's name and the product, in the reader's
 * language ("探索数据 · LEVI"). The root layout's metadata only carries the
 * language-neutral name; the language is a browser-side choice, so this
 * component sets `document.title` from the path.
 */
import { useEffect, useSyncExternalStore } from "react";
import { usePathname } from "next/navigation";
import { useLocale } from "@/components/levi-locale";

export const PRODUCT_TITLE = "LEVI";

function isViewerPath(pathname: string): boolean {
  const parts = pathname.split("/").filter(Boolean);
  if (parts.length === 2) return true;
  return parts.length === 3 && /^episode_\d+$/.test(parts[2]);
}

/** The English page name for a path (the catalog key), or null for the
 * product name alone. */
export function routePageName(pathname: string | null): string | null {
  if (!pathname || pathname === "/") return "Home";
  const first = pathname.split("/").filter(Boolean)[0] ?? "";
  switch (first) {
    case "guide":
      return "Guide";
    case "report":
      return "Report";
    case "explore":
      return "Explore";
    case "workbench":
      return "Conversion & review";
    case "pool":
      return "Training pool";
    case "live":
      return "Live evaluation";
    case "design":
      return "Design system";
    default:
      // /<org>/<dataset> and /<org>/<dataset>/episode_<n>: the episode
      // viewer. Any other address is not a page (the framework's 404).
      return isViewerPath(pathname) ? "Episode viewer" : null;
  }
}

/** "Explore · LEVI"; the viewer adds the dataset and the episode: "Episode
 * viewer · lerobot/aloha · Episode 3 · LEVI". `override` (a catalogue key)
 * replaces the page name: the not-found and error pages name themselves. */
export function routeTitle(
  pathname: string | null,
  translate: (text: string) => string = (text) => text,
  override: string | null = null,
): string {
  if (override) return `${translate(override)} · ${PRODUCT_TITLE}`;
  const name = routePageName(pathname);
  if (!name) return PRODUCT_TITLE;
  const parts = [translate(name)];
  if (name === "Episode viewer" && pathname) {
    const [org, dataset, episode] = pathname.split("/").filter(Boolean);
    if (org && dataset)
      parts.push(`${decodeURIComponent(org)}/${decodeURIComponent(dataset)}`);
    const number = /^episode_(\d+)$/.exec(episode ?? "")?.[1];
    if (number) parts.push(translate(`Episode ${number}`));
  }
  parts.push(PRODUCT_TITLE);
  return parts.join(" · ");
}

// A page the framework answers itself (404, error) cannot be told from the
// path: it names itself here, and RouteTitle shows that.
let override: string | null = null;
const listeners = new Set<() => void>();
export function setTitleOverride(name: string | null) {
  override = name;
  listeners.forEach((listener) => listener());
}
const subscribe = (listener: () => void) => {
  listeners.add(listener);
  return () => void listeners.delete(listener);
};

/** Names the page for the tab title while the component is mounted. */
export function useTitleOverride(name: string) {
  useEffect(() => {
    setTitleOverride(name);
    return () => setTitleOverride(null);
  }, [name]);
}

/**
 * Sets the tab title and keeps it: the framework puts the metadata's title
 * back (it streams it in, after the page has hydrated, and again on a route
 * change), so a title written once is lost. The observer watches the title
 * element, and the places a new one would be inserted, and writes the wanted
 * title again whenever another one lands.
 */
export function RouteTitle() {
  const pathname = usePathname();
  const { t, language } = useLocale();
  const named = useSyncExternalStore(
    subscribe,
    () => override,
    () => null,
  );
  useEffect(() => {
    const want = routeTitle(pathname, t, named);
    let watched: Element | null = null;
    const observer = new MutationObserver(() => sync());
    const watch = () => {
      observer.disconnect();
      observer.observe(document.head, { childList: true });
      observer.observe(document.body, { childList: true });
      watched = document.querySelector("title");
      if (watched)
        observer.observe(watched, {
          childList: true,
          characterData: true,
          subtree: true,
        });
    };
    function sync() {
      if (document.title !== want) document.title = want;
      // A replaced title element needs a new watch.
      if (document.querySelector("title") !== watched) watch();
    }
    watch();
    sync();
    return () => observer.disconnect();
    // `t` is rebuilt on every render; the language is what changes it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pathname, language, named]);
  return null;
}
