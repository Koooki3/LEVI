// Modified for LEVI (2026); see NOTICE and docs/UPSTREAM.md.
"use client";
import { T, useLocale } from "@/components/levi-locale";

import React, { useEffect, useRef, useState } from "react";
import { useAuth } from "@/context/auth-context";

const SIGNIN_BADGE_URL =
  "https://huggingface.co/datasets/huggingface/badges/resolve/main/sign-in-with-huggingface-md-dark.svg";

// `badge` — the official HF brand badge. Use as a strong invitation when the
//           auth path is itself the page's headline action.
// `ghost`  — a quiet inline cyan link, sized to the surrounding body copy.
//           Use when auth is a secondary affordance next to a primary CTA
//           (e.g. the home page's search bar).
// `tab`    — uppercase tracked text styled to match a tab strip; pairs with
//           the episode viewer's tab bar so the auth control reads as part
//           of the same register.
type Variant = "badge" | "ghost" | "tab";

// Slot height per variant. Matches the variant's rendered button so the
// pre-config placeholder (when isAuthAvailable hasn't resolved yet) and the
// signed-in/signed-out states all occupy exactly the same vertical space —
// no layout shift on auth state changes. `tab` is taller because it lives
// in the episode tab bar and needs to align with the `text-xs px-5 py-3`
// tab buttons (~40px implicit height).
const SLOT_HEIGHT: Record<Variant, string> = {
  badge: "h-8",
  ghost: "h-7",
  tab: "h-10",
};

interface HfAuthButtonProps {
  variant?: Variant;
}

export default function HfAuthButton({ variant = "badge" }: HfAuthButtonProps) {
  const { oauth, isAuthAvailable, signIn, signOut } = useAuth();
  const { t } = useLocale();

  // Stable slot — auth state resolves async on mount (config fetch, then
  // localStorage rehydrate), so the rendered control changes from
  // null → signed-out → signed-in. Reserve the height so the surrounding
  // layout doesn't reflow each time.
  if (!isAuthAvailable && !oauth) {
    return <TokenLogin />;
  }

  if (oauth) {
    const name =
      oauth.userInfo?.preferred_username ?? oauth.userInfo?.name ?? "signed in";
    const avatar = oauth.userInfo?.picture;
    return (
      <SignedInMenu
        name={name}
        avatar={avatar}
        onSignOut={signOut}
        variant={variant}
      />
    );
  }

  if (variant === "ghost") {
    return (
      <button
        onClick={signIn}
        title={t("Sign in to access your private datasets")}
        className="cursor-pointer inline-flex items-center h-7 gap-1.5 text-sm tracking-wide text-cyan-300/85 hover:text-cyan-200 transition-colors rounded focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-cyan-400/60"
      >
        <span aria-hidden>🤗</span>
        <span>
          <T>Sign in for private datasets</T>
        </span>
        <span aria-hidden className="opacity-60">
          →
        </span>
      </button>
    );
  }

  if (variant === "tab") {
    return (
      <button
        onClick={signIn}
        title={t("Sign in to access your private datasets")}
        className="cursor-pointer inline-flex items-center h-10 gap-1.5 px-5 text-[11px] font-medium tracking-wide uppercase text-slate-400 hover:text-cyan-300 transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-cyan-400/60"
      >
        <span aria-hidden>🤗</span>
        <span>
          <T>Sign in</T>
        </span>
      </button>
    );
  }

  return (
    <button
      onClick={signIn}
      title={t("Sign in with Hugging Face to access your private datasets")}
      aria-label={t(
        "Sign in with Hugging Face to access your private datasets",
      )}
      className="cursor-pointer inline-flex items-center h-8 rounded-md transition-all duration-150 hover:opacity-90 motion-safe:hover:-translate-y-px focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-cyan-400/60"
    >
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src={SIGNIN_BADGE_URL}
        alt={t("Sign in with Hugging Face")}
        height={32}
        className="h-8 w-auto"
      />
    </button>
  );
}

function SignedInMenu({
  name,
  avatar,
  onSignOut,
  variant,
}: {
  name: string;
  avatar?: string;
  onSignOut: () => void;
  variant: Variant;
}) {
  const [open, setOpen] = useState(false);
  const wrapperRef = useRef<HTMLDivElement>(null);
  const { t } = useLocale();

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (
        wrapperRef.current &&
        !wrapperRef.current.contains(e.target as Node)
      ) {
        setOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div ref={wrapperRef} className="relative inline-flex">
      <button
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={open}
        className={`cursor-pointer inline-flex items-center ${SLOT_HEIGHT[variant]} gap-2 panel-raised bg-[var(--surface-0)]/85 backdrop-blur px-2 text-xs text-slate-300 hover:bg-white/[0.04] transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-cyan-400/60`}
        title={t("Signed in as") + " " + name}
      >
        {avatar && (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={avatar}
            alt=""
            width={22}
            height={22}
            className="rounded-full ring-1 ring-white/10"
          />
        )}
        <span className="tabular max-w-[10rem] truncate">
          <T>{name}</T>
        </span>
        <svg
          aria-hidden
          width="9"
          height="9"
          viewBox="0 0 8 8"
          className={`text-slate-500 transition-transform ${open ? "rotate-180" : ""}`}
        >
          <path d="M1 2.5l3 3 3-3" stroke="currentColor" fill="none" />
        </svg>
      </button>

      {open && (
        <div
          role="menu"
          className="absolute right-0 top-full mt-1.5 min-w-[10rem] panel-raised bg-[var(--surface-1)]/98 backdrop-blur shadow-xl p-1 z-50 text-xs animate-menu-pop"
        >
          <button
            role="menuitem"
            onClick={() => {
              setOpen(false);
              onSignOut();
            }}
            className="cursor-pointer w-full text-left px-2 py-1.5 rounded text-slate-300 hover:bg-white/5 hover:text-slate-100 transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-cyan-400/60"
          >
            <T>Sign out</T>
          </button>
        </div>
      )}
    </div>
  );
}

function TokenLogin() {
  const [open, setOpen] = useState(false),
    [token, setToken] = useState(""),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  const { tokenSignIn } = useAuth();
  const { t } = useLocale();
  return (
    <>
      <button
        className="text-xs text-cyan-300 whitespace-nowrap"
        onClick={() => setOpen(true)}
      >
        <T>Connect Hugging Face</T> ↗
      </button>
      {open && (
        <div
          className="fixed inset-0 z-[100] flex items-center justify-center bg-black/70"
          role="dialog"
          aria-modal="true"
          aria-label={t("Connect Hugging Face")}
        >
          <form
            className="levi-box w-[min(500px,90vw)]"
            onSubmit={async (e) => {
              e.preventDefault();
              setBusy(true);
              setError("");
              try {
                await tokenSignIn(token);
                setToken("");
                setOpen(false);
              } catch (e) {
                setError(String(e));
              } finally {
                setBusy(false);
              }
            }}
          >
            <h2 className="text-lg mb-4">
              <T>Connect Hugging Face</T>
            </h2>
            <p className="text-xs text-slate-400 mb-4">
              <T>
                Your token is stored in this browser for private dataset access.
                Sign out to clear it.
              </T>
            </p>
            <input
              autoFocus
              className="levi-input w-full"
              type="password"
              autoComplete="off"
              aria-label={t("Hugging Face token")}
              placeholder="hf_…"
              required
              value={token}
              onChange={(e) => setToken(e.target.value)}
            />
            {error && (
              <p className="levi-error mt-3">
                <T>{error}</T>
              </p>
            )}
            <div className="levi-row mt-5">
              <button className="levi-primary" disabled={busy}>
                <T>Connect</T>
              </button>
              <button
                type="button"
                className="levi-secondary"
                onClick={() => {
                  setOpen(false);
                  setToken("");
                }}
              >
                <T>Cancel</T>
              </button>
            </div>
          </form>
        </div>
      )}
    </>
  );
}
