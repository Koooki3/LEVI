// Modified for LEVI (2026); see NOTICE and docs/UPSTREAM.md.
"use client";
import { T } from "@/components/levi-locale";

import React, {
  createContext,
  useContext,
  useEffect,
  useState,
  useCallback,
} from "react";
import {
  oauthLoginUrl,
  oauthHandleRedirectIfPresent,
  type OAuthResult,
} from "@huggingface/hub";
import { AUTH_STORAGE_KEY, clearLegacyAuthStorage } from "@/utils/auth";
import {
  readBrowserStorage,
  removeBrowserStorage,
  writeBrowserStorage,
} from "@/utils/browserStorage";

interface OAuthAppConfig {
  clientId: string;
  scopes: string;
}

interface AuthContextValue {
  oauth: OAuthResult | null;
  // Whether OAuth is configured for this deployment. Determined by hitting
  // /api/auth/config — the server reads OAUTH_CLIENT_ID from its env, which
  // HF Spaces injects when `hf_oauth: true` is set in the README. When
  // unconfigured, the button hides itself.
  isAuthAvailable: boolean;
  signIn: () => Promise<void>;
  signOut: () => void;
  tokenSignIn: (token: string) => Promise<void>;
}

const AuthContext = createContext<AuthContextValue>({
  oauth: null,
  isAuthAvailable: false,
  signIn: async () => {},
  signOut: () => {},
  tokenSignIn: async () => {},
});

// Mirror the access token into an HttpOnly cookie so the same-origin
// /api/proxy route can attach it to <video> requests, which can't carry an
// Authorization header from JS.
async function setSessionCookie(accessToken: string): Promise<void> {
  const response = await fetch("/api/auth/session", {
    method: "POST",
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  if (!response.ok) throw new Error("LEVI session could not be established");
}

async function clearSessionCookie(): Promise<void> {
  try {
    await fetch("/api/auth/session", { method: "DELETE" });
  } catch (err) {
    console.error("Failed to clear session cookie", err);
  }
}

function notifyAuthChanged(): void {
  if (typeof window !== "undefined")
    window.dispatchEvent(new Event("levi:hf-auth-changed"));
}

function isExpired(result: OAuthResult): boolean {
  const exp = result.accessTokenExpiresAt;
  if (!exp) return false;
  const expDate = exp instanceof Date ? exp : new Date(exp);
  return expDate.getTime() <= Date.now();
}

async function fetchOAuthConfig(): Promise<OAuthAppConfig | null> {
  try {
    const res = await fetch("/api/auth/config");
    if (!res.ok) return null;
    const data = (await res.json()) as
      | { enabled: false }
      | { enabled: true; clientId: string; scopes: string };
    if (!data.enabled) return null;
    return { clientId: data.clientId, scopes: data.scopes };
  } catch {
    return null;
  }
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [oauth, setOauth] = useState<OAuthResult | null>(null);
  const [config, setConfig] = useState<OAuthAppConfig | null>(null);

  useEffect(() => {
    let cancelled = false;
    // Remove the previous visualizer's namespace before any async request so
    // a failed/offline config fetch cannot leave the old account selected.
    clearLegacyAuthStorage();

    fetchOAuthConfig().then((cfg) => {
      if (cancelled) return;
      setConfig(cfg);

      const stored = readBrowserStorage("local", AUTH_STORAGE_KEY);
      if (stored) {
        try {
          const parsed = JSON.parse(stored) as OAuthResult;
          if (isExpired(parsed)) {
            removeBrowserStorage("local", AUTH_STORAGE_KEY);
            void clearSessionCookie().finally(notifyAuthChanged);
          } else {
            setOauth(parsed);
            void setSessionCookie(parsed.accessToken)
              .then(() => {
                if (!cancelled) notifyAuthChanged();
              })
              .catch((err) => {
                if (cancelled) return;
                console.error("Stored Hugging Face session is invalid", err);
                removeBrowserStorage("local", AUTH_STORAGE_KEY);
                setOauth(null);
                notifyAuthChanged();
              });
            return;
          }
        } catch {
          removeBrowserStorage("local", AUTH_STORAGE_KEY);
          void clearSessionCookie().finally(notifyAuthChanged);
        }
      }

      if (!cfg) return;
      oauthHandleRedirectIfPresent()
        .then((result) => {
          if (cancelled || !result) return;
          return setSessionCookie(result.accessToken).then(() => {
            if (cancelled) return;
            writeBrowserStorage(
              "local",
              AUTH_STORAGE_KEY,
              JSON.stringify(result),
            );
            setOauth(result);
            notifyAuthChanged();
          });
        })
        .catch((err) => {
          console.error("OAuth redirect handling failed", err);
        });
    });

    return () => {
      cancelled = true;
    };
  }, []);

  const signIn = useCallback(async () => {
    if (!config) return;
    const url = await oauthLoginUrl({
      clientId: config.clientId,
      scopes: config.scopes,
    });
    window.location.href = url + "&prompt=consent";
  }, [config]);

  const tokenSignIn = useCallback(async (value: string) => {
    const accessToken = value.trim();
    if (!accessToken) throw new Error("Enter a Hugging Face token");
    const response = await fetch("https://huggingface.co/api/whoami-v2", {
      headers: { Authorization: `Bearer ${accessToken}` },
    });
    if (!response.ok) throw new Error("Hugging Face token rejected");
    const user = await response.json();
    const result = {
      accessToken,
      userInfo: {
        preferred_username: user.name,
        name: user.name,
        picture: user.avatarUrl,
      },
    } as OAuthResult;
    await setSessionCookie(accessToken);
    clearLegacyAuthStorage();
    writeBrowserStorage("local", AUTH_STORAGE_KEY, JSON.stringify(result));
    setOauth(result);
    notifyAuthChanged();
  }, []);

  const signOut = useCallback(() => {
    removeBrowserStorage("local", AUTH_STORAGE_KEY);
    clearLegacyAuthStorage();
    setOauth(null);
    void clearSessionCookie().finally(notifyAuthChanged);
    // Strip ?code=... left in the URL by the OAuth redirect, if any.
    const cleanUrl = window.location.href.replace(/\?.*$/, "");
    if (cleanUrl !== window.location.href) {
      window.history.replaceState(null, "", cleanUrl);
    }
  }, []);

  return (
    <T>
      {
        <AuthContext.Provider
          value={{
            oauth,
            isAuthAvailable: !!config,
            signIn,
            signOut,
            tokenSignIn,
          }}
        >
          <T>{children}</T>
        </AuthContext.Provider>
      }
    </T>
  );
}

export function useAuth() {
  return useContext(AuthContext);
}
