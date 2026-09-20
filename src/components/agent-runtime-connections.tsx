"use client";
import { useEffect, useState } from "react";
import { useLocale } from "./levi-locale";

type Profile = {
  id: string;
  installed: boolean;
  version: string;
  adapter_installed?: boolean;
  adapter_version?: string;
  cli_installed?: boolean;
  cli_version?: string | null;
  authentication: string;
  login_command: string;
  logout_command: string;
};
type Grant = {
  id: string;
  client: string;
  datasets: string[];
  enabled: boolean;
  expires: number;
  calls: number;
  max_tool_calls: number;
};
export default function AgentRuntimeConnections() {
  const { t } = useLocale();
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [grants, setGrants] = useState<Grant[]>([]);
  const [error, setError] = useState("");
  async function refresh() {
    try {
      const responses = await Promise.all([
        fetch("/api/levi/agent/v1/runtimes"),
        fetch("/api/levi/agent/v1/grants"),
      ]);
      if (responses.some((r) => !r.ok))
        throw new Error("Runtime connection status unavailable");
      const [p, g] = await Promise.all(responses.map((r) => r.json()));
      setProfiles(Array.isArray(p) ? p : []);
      setGrants(Array.isArray(g) ? g : []);
    } catch (e) {
      setError(String(e));
    }
  }
  useEffect(() => {
    void refresh();
  }, []);
  return (
    <details className="levi-pilot">
      <summary>{t("Codex / Claude connections")}</summary>
      <p>
        {t(
          "Login is owned by the official local client. LEVI does not copy account credentials.",
        )}
      </p>
      <button onClick={() => void refresh()}>{t("Refresh status")}</button>
      {profiles.map((p) => (
        <article key={p.id}>
          <h4>{p.id}</h4>
          <p>
            {t("This machine")}:{" "}
            {p.cli_installed
              ? p.cli_version
              : t("command not found on the service's PATH")}
          </p>
          <p>
            {t("LEVI adapter")}:{" "}
            {t(
              (p.adapter_installed ?? p.installed)
                ? "installed"
                : "not installed",
            )}{" "}
            · {t("pinned")} {p.adapter_version ?? p.version}
          </p>
          <p className="levi-agent-muted">
            {t(
              "Login is checked by the official client, not by LEVI, so it is not reported here.",
            )}
          </p>
          <p>
            {t("Official login / switch account")}:{" "}
            <code>{p.login_command}</code>
          </p>
          <p>
            {t("Official logout affects other local clients")}:{" "}
            <code>{p.logout_command}</code>
          </p>
        </article>
      ))}
      <p>
        {t(
          "Use levi agent connect to review and apply project MCP configuration.",
        )}
      </p>
      {grants.map((g) => (
        <article key={g.id}>
          <strong>{g.client}</strong> · {g.datasets.join(", ")}
          <p>
            {t(
              g.enabled && g.expires * 1000 > Date.now()
                ? "Connected"
                : "Disconnected",
            )}{" "}
            · {g.calls}/{g.max_tool_calls} ·{" "}
            {new Date(g.expires * 1000).toLocaleString()}
          </p>
          <button
            disabled={!g.enabled}
            onClick={async () => {
              const r = await fetch(
                `/api/levi/agent/v1/grants/${g.id}/revoke`,
                { method: "POST" },
              );
              if (!r.ok) setError(t("Disconnect failed"));
              else await refresh();
            }}
          >
            {t("Disconnect LEVI only")}
          </button>
        </article>
      ))}
      {error && <p role="status">{error}</p>}
    </details>
  );
}
