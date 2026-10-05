// Modified for LEVI (2026); see NOTICE and docs/UPSTREAM.md.
"use client";
import { T, useLocale } from "@/components/levi-locale";

import React, { useState } from "react";
import { ArrowUpRight, LogIn, LogOut, Repeat } from "lucide-react";
import { useAuth } from "@/context/auth-context";
import { Button, Dialog, Field, Input, Menu, Tooltip } from "@/components/ds";
import { Problem } from "@/components/pages-ui/feedback";
import "@/components/pages-ui/shared.css";

const SIGNIN_BADGE_URL =
  "https://huggingface.co/datasets/huggingface/badges/resolve/main/sign-in-with-huggingface-md-dark.svg";

// `badge` — the official HF brand badge. Use as a strong invitation when the
//           auth path is itself the page's headline action.
// `ghost`  — a quiet inline link-button, sized to the surrounding body copy.
//           Use when auth is a secondary affordance next to a primary CTA
//           (e.g. the home page's search bar).
// `tab`    — a quiet button for the episode viewer's tab bar, so the auth
//           control reads as part of the same strip.
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
  const [switching, setSwitching] = useState(false);
  if (switching)
    return <TokenLogin initiallyOpen onClose={() => setSwitching(false)} />;

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
        onSwitch={() => setSwitching(true)}
        variant={variant}
      />
    );
  }

  if (variant === "ghost" || variant === "tab") {
    return (
      <Tooltip content={t("Sign in to access your private datasets")}>
        <Button
          variant="ghost"
          size={variant === "ghost" ? "sm" : "md"}
          icon={LogIn}
          className={`levi-hf-auth levi-hf-auth--${variant} ${SLOT_HEIGHT[variant]}`}
          onClick={signIn}
        >
          <T>
            {variant === "ghost" ? "Sign in for private datasets" : "Sign in"}
          </T>
        </Button>
      </Tooltip>
    );
  }

  return (
    <Tooltip
      content={t("Sign in with Hugging Face to access your private datasets")}
    >
      <button
        type="button"
        onClick={signIn}
        aria-label={t(
          "Sign in with Hugging Face to access your private datasets",
        )}
        className="levi-hf-auth-badge ds-focus"
      >
        {/* The official Hugging Face sign-in badge (brand artwork). */}
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          src={SIGNIN_BADGE_URL}
          alt={t("Sign in with Hugging Face")}
          height={32}
        />
      </button>
    </Tooltip>
  );
}

function SignedInMenu({
  name,
  avatar,
  onSignOut,
  onSwitch,
  variant,
}: {
  name: string;
  avatar?: string;
  onSignOut: () => void;
  onSwitch: () => void;
  variant: Variant;
}) {
  const { t } = useLocale();
  return (
    <span className={`levi-hf-auth levi-hf-auth--${variant}`}>
      <Menu
        variant="ghost"
        align="end"
        ariaLabel={`${t("Signed in as")} ${name}`}
        tooltip={`${t("Signed in as")} ${name}`}
        label={
          <span className="levi-hf-auth-user">
            {avatar && (
              // eslint-disable-next-line @next/next/no-img-element
              <img src={avatar} alt="" width={22} height={22} />
            )}
            <span className="tabular">{name}</span>
          </span>
        }
        items={[
          {
            id: "switch",
            label: t("Switch account"),
            icon: Repeat,
            onSelect: onSwitch,
          },
          {
            id: "signout",
            label: t("Sign out"),
            icon: LogOut,
            onSelect: onSignOut,
          },
        ]}
      />
    </span>
  );
}

function TokenLogin({
  initiallyOpen = false,
  onClose,
}: { initiallyOpen?: boolean; onClose?: () => void } = {}) {
  const [open, setOpen] = useState(initiallyOpen),
    [token, setToken] = useState(""),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  const { tokenSignIn } = useAuth();
  const { t } = useLocale();
  const close = () => {
    setOpen(false);
    setToken("");
    setError("");
    onClose?.();
  };
  return (
    <>
      <Button
        variant="ghost"
        size="sm"
        className="levi-hf-auth"
        iconEnd={ArrowUpRight}
        onClick={() => setOpen(true)}
      >
        <T>Connect Hugging Face</T>
      </Button>
      <Dialog
        open={open}
        onClose={close}
        size="sm"
        title={t("Connect Hugging Face")}
        description={t(
          "Your token is stored in this browser for private dataset access. Sign out to clear it.",
        )}
      >
        <form
          className="levi-hf-token-form"
          onSubmit={async (e) => {
            e.preventDefault();
            setBusy(true);
            setError("");
            try {
              await tokenSignIn(token);
              setToken("");
              setOpen(false);
              onClose?.();
            } catch (e) {
              setError(String(e));
            } finally {
              setBusy(false);
            }
          }}
        >
          <Field label={t("Hugging Face token")} required>
            <Input
              autoFocus
              type="password"
              autoComplete="off"
              placeholder="hf_…"
              required
              value={token}
              onChange={(e) => setToken(e.target.value)}
            />
          </Field>
          {error && (
            <Problem
              title={t("The token was not accepted")}
              why={t(error)}
              fix={t("Check the token on huggingface.co and paste it again.")}
            />
          )}
          <div className="levi-hf-token-actions">
            <Button variant="ghost" onClick={close}>
              <T>Cancel</T>
            </Button>
            <Button type="submit" variant="primary" loading={busy}>
              <T>Connect</T>
            </Button>
          </div>
        </form>
      </Dialog>
    </>
  );
}
