"use client";
import { useEffect, useState } from "react";
import { useAuth } from "@/context/auth-context";
import HfAuthButton from "./hf-auth-button";
import { T, useLocale } from "./levi-locale";

export type Connection = {
  name: string;
  model: string;
  base_url: string;
  vision: boolean;
  tools: boolean;
  enabled: boolean;
  credential_ready: boolean;
  credential_source: string;
  key_env: string;
  allow_localhost: boolean;
};
export default function AgentConnections({
  providers,
  selected,
  select,
  refresh,
  edit,
}: {
  providers: Connection[];
  selected: string;
  select: (name: string) => void;
  refresh: () => Promise<void>;
  edit: (p: Connection) => void;
}) {
  const [external, setExternal] = useState<{
    configured: boolean;
    enabled: boolean;
    datasets: string[];
  } | null>(null);
  useEffect(() => {
    let cancelled = false;
    void fetch("/api/levi/agent/v1/connections")
      .then((r) => r.json())
      .then((r) => {
        if (!cancelled) setExternal(r.external || null);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);
  const { oauth } = useAuth();
  const { t } = useLocale();
  const [credentialFor, setCredentialFor] = useState<string | null>(null);
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [remove, setRemove] = useState<string | null>(null);
  async function action(name: string, operation: string, body?: unknown) {
    setBusy(true);
    setError("");
    try {
      const res = await fetch(
        `/api/levi/agent/v1/providers/${encodeURIComponent(name)}${operation ? "/" + operation : ""}`,
        {
          method: operation ? "POST" : "DELETE",
          headers: { "Content-Type": "application/json" },
          body: body ? JSON.stringify(body) : undefined,
        },
      );
      if (!res.ok) {
        const detail = await res.json();
        throw new Error(detail.detail || "Connection update failed");
      }
      setKey("");
      setCredentialFor(null);
      setRemove(null);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <T>
      <section className="levi-connections" aria-label="Accounts & connections">
        <h3>Accounts & connections</h3>
        {error && (
          <p role="alert" className="levi-agent-error">
            {error}
          </p>
        )}
        <article className="levi-connection-card">
          <div className="levi-connection-title">
            <span className="levi-connection-avatar">HF</span>
            <div>
              <strong>Hugging Face</strong>
              <p>
                {oauth
                  ? oauth.userInfo?.preferred_username || oauth.userInfo?.name
                  : t("Signed out")}
              </p>
            </div>
            <span className={`levi-connection-status ${oauth ? "ready" : ""}`}>
              {t(oauth ? "Signed in" : "Signed out")}
            </span>
          </div>
          <p className="levi-agent-muted">
            Dataset and checkpoint access. This account does not grant Agent
            commit permissions.
          </p>
          <div className="levi-agent-actions">
            <HfAuthButton variant="ghost" />
          </div>
        </article>
        {providers.map((p) => (
          <article
            key={p.name}
            className={`levi-connection-card ${p.name === selected ? "selected" : ""}`}
          >
            <div className="levi-connection-title">
              <span className="levi-connection-avatar">
                {p.name.slice(0, 2).toUpperCase()}
              </span>
              <div>
                <strong>{p.name}</strong>
                <p>{p.model}</p>
              </div>
              <span
                className={`levi-connection-status ${p.enabled && p.credential_ready ? "ready" : ""}`}
              >
                {t(
                  !p.enabled
                    ? "Disconnected"
                    : p.credential_ready
                      ? "Configured"
                      : "Credential missing",
                )}
              </span>
            </div>
            <p className="levi-agent-endpoint">{p.base_url}</p>
            <p className="levi-agent-muted">
              {t(p.vision ? "Image + text" : "Text only")} ·{" "}
              {t(
                p.credential_source === "session"
                  ? "Session credential"
                  : p.credential_source === "environment"
                    ? "Environment credential"
                    : "Credential missing",
              )}
            </p>
            <div className="levi-agent-actions">
              <button
                disabled={busy || !p.enabled}
                aria-pressed={selected === p.name}
                onClick={() => select(p.name)}
              >
                {t(selected === p.name ? "Selected" : "Use this model")}
              </button>
              <button disabled={busy} onClick={() => edit(p)}>
                Edit configuration
              </button>
              <button
                disabled={busy}
                onClick={() => {
                  setCredentialFor(p.name);
                  setKey("");
                }}
              >
                {t("Set session credential")}
              </button>
              <button
                disabled={busy}
                onClick={() =>
                  void action(p.name, p.enabled ? "disconnect" : "activate")
                }
              >
                {t(p.enabled ? "Disconnect" : "Reconnect")}
              </button>
              <button disabled={busy} onClick={() => setRemove(p.name)}>
                Remove configuration
              </button>
            </div>
            {credentialFor === p.name && (
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  void action(p.name, "session", { key });
                }}
              >
                <label>
                  Session API key
                  <input
                    type="password"
                    autoComplete="off"
                    value={key}
                    onChange={(e) => setKey(e.target.value)}
                    required
                  />
                </label>
                <p className="levi-agent-muted">
                  Held in server memory only; cleared on disconnect or restart.
                </p>
                <button disabled={busy}>Save session credential</button>
                <button
                  type="button"
                  onClick={() => {
                    setCredentialFor(null);
                    setKey("");
                  }}
                >
                  Cancel
                </button>
              </form>
            )}
            {remove === p.name && (
              <div role="alert">
                <p>
                  Remove this configuration? Existing task history will be
                  preserved.
                </p>
                <button disabled={busy} onClick={() => void action(p.name, "")}>
                  Remove configuration
                </button>
                <button onClick={() => setRemove(null)}>Cancel</button>
              </div>
            )}
          </article>
        ))}
        <article className="levi-connection-card">
          <div className="levi-connection-title">
            <span className="levi-connection-avatar">MCP</span>
            <strong>External Agent</strong>
            <span
              className={`levi-connection-status ${external?.configured && external.enabled ? "ready" : ""}`}
            >
              {t(
                external?.configured && external.enabled
                  ? "Configured"
                  : "Disconnected",
              )}
            </span>
          </div>
          <p className="levi-agent-muted">
            External credentials and dataset scopes are server-managed. HF login
            does not change these permissions.
          </p>
          {external?.datasets?.length ? (
            <p>{external.datasets.join(" · ")}</p>
          ) : (
            <p>No external dataset scope configured.</p>
          )}
          <button
            disabled={busy || !external?.configured}
            onClick={async () => {
              if (!external) return;
              setBusy(true);
              setError("");
              try {
                const response = await fetch(
                  "/api/levi/agent/v1/connections/external",
                  {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ enabled: !external.enabled }),
                  },
                );
                if (!response.ok) throw new Error("Connection update failed");
                setExternal({ ...external, enabled: !external.enabled });
              } catch (e) {
                setError(String(e));
              } finally {
                setBusy(false);
              }
            }}
          >
            {t(
              external?.enabled
                ? "Disconnect external Agent"
                : "Reconnect external Agent",
            )}
          </button>
        </article>
      </section>
    </T>
  );
}
