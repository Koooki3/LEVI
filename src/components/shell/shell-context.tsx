"use client";
/**
 * State the global frame shares: the theme preference (one instance, applied
 * to <html>; without a stored choice it is dark until design stage 5, see
 * THEME_DEFAULT_PREFERENCE), and whether the command palette or the shortcut list is open.
 * It also listens for the frame's own shortcuts (global-keys.ts).
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import {
  applyTheme,
  THEME_DEFAULT_PREFERENCE,
  useThemePreference,
  type ResolvedTheme,
  type ThemePreference,
} from "@/lib/design/theme";
import { openModalDialog } from "./confirm";
import {
  applyMotion,
  useMotionPreference,
  type MotionPreference,
} from "./motion-preference";
import { globalShortcut } from "./global-keys";

type Shell = {
  theme: ThemePreference;
  resolvedTheme: ResolvedTheme;
  setTheme: (theme: ThemePreference) => void;
  motion: MotionPreference;
  setMotion: (motion: MotionPreference) => void;
  paletteOpen: boolean;
  setPaletteOpen: (open: boolean) => void;
  shortcutsOpen: boolean;
  setShortcutsOpen: (open: boolean) => void;
};

const ShellContext = createContext<Shell | null>(null);

/** The frame's own dialogs carry this class; any other modal blocks its keys. */
export const SHELL_OVERLAY_CLASS = "levi-shell-overlay";
const OTHER_MODAL = `[aria-modal="true"]:not(.${SHELL_OVERLAY_CLASS})`;

const OUTSIDE: Shell = {
  theme: THEME_DEFAULT_PREFERENCE,
  resolvedTheme: "dark",
  setTheme: () => undefined,
  motion: "system",
  setMotion: () => undefined,
  paletteOpen: false,
  setPaletteOpen: () => undefined,
  shortcutsOpen: false,
  setShortcutsOpen: () => undefined,
};

export function useShell(): Shell {
  return useContext(ShellContext) ?? OUTSIDE;
}

export function ShellProvider({ children }: { children: ReactNode }) {
  const { preference, resolved, setPreference, ready } = useThemePreference();
  const motion = useMotionPreference();
  const [paletteOpen, setPaletteOpenState] = useState(false);
  const [shortcutsOpen, setShortcutsOpenState] = useState(false);

  // Only once the stored value is read: before that `preference` is the
  // default, and applying it would overwrite what the boot script set.
  useEffect(() => {
    if (ready) applyTheme(document.documentElement, preference);
  }, [preference, ready]);

  useEffect(() => {
    if (motion.ready) applyMotion(document.documentElement, motion.preference);
  }, [motion.preference, motion.ready]);

  const setPaletteOpen = useCallback((open: boolean) => {
    setPaletteOpenState(open);
    if (open) setShortcutsOpenState(false);
  }, []);
  const setShortcutsOpen = useCallback((open: boolean) => {
    setShortcutsOpenState(open);
    if (open) setPaletteOpenState(false);
  }, []);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return;
      const shortcut = globalShortcut(event);
      if (!shortcut) return;
      // Another dialog (a confirmation, a page's own modal, a native modal
      // <dialog> such as the training pool's push dialog) keeps the keys.
      if (document.querySelector(OTHER_MODAL) || openModalDialog()) return;
      event.preventDefault();
      if (shortcut === "palette") {
        setPaletteOpenState((open) => !open);
        setShortcutsOpenState(false);
      } else {
        setShortcutsOpenState(true);
        setPaletteOpenState(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const value = useMemo(
    () => ({
      theme: preference,
      resolvedTheme: resolved,
      setTheme: setPreference,
      motion: motion.preference,
      setMotion: motion.setPreference,
      paletteOpen,
      setPaletteOpen,
      shortcutsOpen,
      setShortcutsOpen,
    }),
    [
      preference,
      resolved,
      setPreference,
      motion.preference,
      motion.setPreference,
      paletteOpen,
      setPaletteOpen,
      shortcutsOpen,
      setShortcutsOpen,
    ],
  );
  return (
    <ShellContext.Provider value={value}>{children}</ShellContext.Provider>
  );
}
