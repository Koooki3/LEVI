"use client";
import { useEffect, useState } from "react";
import { RefreshCw, Unplug, Terminal } from "lucide-react";
import { useLocale } from "./levi-locale";
import { Badge, Button, Card } from "@/components/ds";
import { RequestProblem } from "@/components/pages-ui/feedback";
import { Actions, Disclosure, GatedButton, Hint } from "./agent-ui";

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
    <Disclosure summary={t("Codex / Claude connections")} icon={Terminal}>
      <div className="ag-stack">
        <Hint>
          {t(
            "Login is owned by the official local client. LEVI does not copy account credentials.",
          )}
        </Hint>
        <Actions>
          <Button size="sm" icon={RefreshCw} onClick={() => void refresh()}>
            {t("Refresh status")}
          </Button>
        </Actions>
        {profiles.map((p) => (
          <Card key={p.id} padding="compact" title={p.id}>
            <dl className="ag-facts">
              <dt>{t("This machine")}</dt>
              <dd>
                {p.cli_installed
                  ? p.cli_version
                  : t("command not found on the service's PATH")}
              </dd>
              <dt>{t("LEVI adapter")}</dt>
              <dd>
                {t(
                  (p.adapter_installed ?? p.installed)
                    ? "installed"
                    : "not installed",
                )}{" "}
                · {t("pinned")} {p.adapter_version ?? p.version}
              </dd>
              <dt>{t("Official login / switch account")}</dt>
              <dd>
                <code>{p.login_command}</code>
              </dd>
              <dt>{t("Official logout affects other local clients")}</dt>
              <dd>
                <code>{p.logout_command}</code>
              </dd>
            </dl>
            <Hint>
              {t(
                "Login is checked by the official client, not by LEVI, so it is not reported here.",
              )}
            </Hint>
          </Card>
        ))}
        <Hint>
          {t(
            "Use levi agent connect to review and apply project MCP configuration.",
          )}
        </Hint>
        {grants.map((g) => {
          const live = g.enabled && g.expires * 1000 > Date.now();
          return (
            <Card
              key={g.id}
              padding="compact"
              title={g.client}
              description={g.datasets.join(", ")}
              actions={
                <Badge tone={live ? "success" : "neutral"}>
                  {t(live ? "Connected" : "Disconnected")}
                </Badge>
              }
            >
              <p className="ag-muted">
                {g.calls}/{g.max_tool_calls} ·{" "}
                {new Date(g.expires * 1000).toLocaleString()}
              </p>
              <Actions>
                <GatedButton
                  size="sm"
                  icon={Unplug}
                  reason={!g.enabled ? t("Already disconnected.") : null}
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
                </GatedButton>
              </Actions>
            </Card>
          );
        })}
        {error && (
          <RequestProblem
            action="The connection status could not be read"
            message={error}
          />
        )}
      </div>
    </Disclosure>
  );
}
