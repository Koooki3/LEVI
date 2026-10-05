// Modified for LEVI (2026); see NOTICE and docs/UPSTREAM.md.
"use client";
import { T, useLocale } from "@/components/levi-locale";

import React, { useState } from "react";
import { ArrowUpRight, LogIn, LogOut, Repeat, UserRound } from "lucide-react";
import { useAuth } from "@/context/auth-context";
import {
  Button,
  Dialog,
  Field,
  Icon,
  Input,
  Menu,
  Tooltip,
} from "@/components/ds";
import { Problem } from "@/components/pages-ui/feedback";
import "@/components/pages-ui/shared.css";

// `badge` — the sign-in as a headline action: a ds Button with an icon and the
//           words "Sign in with Hugging Face". It used to be the official dark
//           badge image fetched from huggingface.co; that image stayed dark in
//           the light theme and made every page load a request to a third
//           party, so the button is drawn by the design system instead.
// `ghost`  — a quiet inline link-button, sized to the surrounding body copy.
//           Use when auth is a secondary affordance next to a primary CTA
//           (e.g. the home page's search bar).
// `tab`    — a quiet button for the episode viewer's tab bar, so the auth
//           control reads as part of the same strip.
type Variant = "badge" | "ghost" | "tab";

const sizeOf = (variant: Variant) =>
  variant === "ghost" ? "sm" : variant === "tab" ? "lg" : "md";

interface HfAuthButtonProps {
  variant?: Variant;
}

export default function HfAuthButton({ variant = "badge" }: HfAuthButtonProps) {
  const { oauth, isAuthAvailable, signIn, signOut } = useAuth();
  const { t } = useLocale();
  const [switching, setSwitching] = useState(false);
  if (switching)
    return (
      <TokenLogin
        variant={variant}
        initiallyOpen
        onClose={() => setSwitching(false)}
      />
    );

  // Auth state resolves async on mount (config fetch, then localStorage
  // rehydrate), so the control changes from the token login to the sign-in
  // button or the signed-in menu. All of them are ds buttons of the same size
  // per variant, so the surrounding layout does not reflow.
  if (!isAuthAvailable && !oauth) {
    return <TokenLogin variant={variant} />;
  }

  if (oauth) {
    const name =
      oauth.userInfo?.preferred_username ?? oauth.userInfo?.name ?? "signed in";
    return (
      <SignedInMenu
        name={name}
        onSignOut={signOut}
        onSwitch={() => setSwitching(true)}
        variant={variant}
      />
    );
  }

  return (
    <Tooltip
      content={t(
        variant === "badge"
          ? "Sign in with Hugging Face to access your private datasets"
          : "Sign in to access your private datasets",
      )}
    >
      <Button
        variant={variant === "badge" ? "secondary" : "ghost"}
        size={sizeOf(variant)}
        icon={LogIn}
        className={`levi-hf-auth levi-hf-auth--${variant}`}
        onClick={signIn}
      >
        <T>
          {variant === "ghost"
            ? "Sign in for private datasets"
            : variant === "tab"
              ? "Sign in"
              : "Sign in with Hugging Face"}
        </T>
      </Button>
    </Tooltip>
  );
}

function SignedInMenu({
  name,
  onSignOut,
  onSwitch,
  variant,
}: {
  name: string;
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
            <Icon icon={UserRound} />
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
  variant,
  initiallyOpen = false,
  onClose,
}: {
  variant: Variant;
  initiallyOpen?: boolean;
  onClose?: () => void;
}) {
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
        size={sizeOf(variant)}
        className={`levi-hf-auth levi-hf-auth--${variant}`}
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
