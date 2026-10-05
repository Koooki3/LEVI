"use client";
import { useEffect, useState } from "react";
import { useAuth } from "@/context/auth-context";
import OllamaRuntime from "./ollama-runtime";
import OllamaModels from "./ollama-models";
import HfAuthButton from "./hf-auth-button";
import {
  Check,
  Cloud,
  Cpu,
  KeyRound,
  Pencil,
  Plug,
  Server,
  Trash2,
  Unplug,
  UserRound,
} from "lucide-react";
import { T, useLocale } from "./levi-locale";
import { Badge, Button, Card, Field, Input, type Tone } from "@/components/ds";
import { RequestProblem } from "@/components/pages-ui/feedback";
import { useConfirmAction } from "./shell/confirm";
import { Actions, ConnectionHead, GatedButton, Hint } from "./agent-ui";

export type Connection = {
  kind?: "openai-compatible" | "ollama" | "openai-local";
  model_digest?: string | null;
  context_tokens?: number;
  max_images?: number | null;
  image_max_side?: number | null;
  think?: boolean;
  fold_system?: boolean;
  prompt_style?: "full" | "lean";
  requests_in_flight?: number;
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
    shared_token?: boolean;
    live?: boolean;
    grants?: Array<{
      id: string;
      label: string;
      datasets: string[];
      calls: number;
      expires_in_seconds: number | null;
      idle_seconds: number | null;
      live: boolean;
    }>;
  } | null>(null);
  useEffect(() => {
    let cancelled = false;
    // A scoped connection can be created or expire while this panel is open,
    // so the card follows the server rather than a value read once.
    const load = () =>
      fetch("/api/levi/agent/v1/connections")
        .then((r) => r.json())
        .then((r) => {
          if (!cancelled) setExternal(r.external || null);
        })
        .catch(() => {});
    void load();
    const timer = setInterval(load, 5000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);
  const { oauth } = useAuth();
  const { t } = useLocale();
  const confirm = useConfirmAction();
  const [credentialFor, setCredentialFor] = useState<string | null>(null);
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
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
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }
  async function remove(name: string) {
    // The configuration goes, the task history stays: ask before it goes.
    const yes = await confirm({
      title: t("Remove this configuration?"),
      description: `${name} · ${t("Existing task history will be preserved.")}`,
      confirmLabel: t("Remove configuration"),
      tone: "danger",
    });
    if (yes) await action(name, "");
  }
  const providerStatus = (p: Connection): { tone: Tone; text: string } =>
    !p.enabled
      ? { tone: "neutral", text: "Disconnected" }
      : p.credential_ready
        ? { tone: "success", text: "Configured" }
        : { tone: "warning", text: "Credential missing" };
  const externalStatus = (): { tone: Tone; text: string } =>
    !external?.enabled
      ? { tone: "neutral", text: "Disconnected" }
      : external.live
        ? { tone: "success", text: "Connected" }
        : external.grants?.length
          ? { tone: "info", text: "Waiting for the agent to call" }
          : external.configured
            ? { tone: "success", text: "Configured" }
            : { tone: "neutral", text: "Disconnected" };
  const ext = externalStatus();
  return (
    <T>
      <section className="ag-section" aria-label={t("Accounts & connections")}>
        <h3>{t("Accounts & connections")}</h3>
        {error && (
          <RequestProblem
            action="The connection was not updated"
            message={error}
          />
        )}
        <Card padding="compact" className="ag-conn">
          <ConnectionHead
            icon={UserRound}
            title="Hugging Face"
            subtitle={
              oauth
                ? oauth.userInfo?.preferred_username || oauth.userInfo?.name
                : t("Signed out")
            }
            status={
              <Badge tone={oauth ? "success" : "neutral"}>
                {t(oauth ? "Signed in" : "Signed out")}
              </Badge>
            }
          />
          <Hint>
            Dataset and checkpoint access. This account does not grant Agent
            commit permissions.
          </Hint>
          <Actions>
            <HfAuthButton variant="ghost" />
          </Actions>
        </Card>
        <OllamaRuntime
          configured={refresh}
          connectionExists={providers.some((p) => p.name === "ollama-managed")}
        />
        {providers.map((p) => {
          const status = providerStatus(p);
          return (
            <Card
              key={p.name}
              padding="compact"
              className={`ag-conn ${p.name === selected ? "is-selected" : ""}`}
            >
              <ConnectionHead
                icon={p.kind === "openai-compatible" ? Cloud : Cpu}
                title={p.name}
                subtitle={p.model}
                status={<Badge tone={status.tone}>{t(status.text)}</Badge>}
              />
              <p className="ag-endpoint">{p.base_url}</p>
              <p className="ag-muted">
                {t(p.vision ? "Image + text" : "Text only")} ·{" "}
                {t(
                  p.credential_source === "not_required"
                    ? "No API key required"
                    : p.credential_source === "session"
                      ? "Session credential"
                      : p.credential_source === "environment"
                        ? "Environment credential"
                        : "Credential missing",
                )}
              </p>
              <Actions>
                <GatedButton
                  size="sm"
                  icon={selected === p.name ? Check : undefined}
                  aria-pressed={selected === p.name}
                  disabled={busy}
                  reason={
                    !p.enabled
                      ? t("Reconnect this model before you use it.")
                      : null
                  }
                  onClick={() => select(p.name)}
                >
                  {t(selected === p.name ? "Selected" : "Use this model")}
                </GatedButton>
                <Button
                  size="sm"
                  icon={Pencil}
                  disabled={busy}
                  onClick={() => edit(p)}
                >
                  {t("Edit configuration")}
                </Button>
                {p.kind !== "ollama" && (
                  <Button
                    size="sm"
                    icon={KeyRound}
                    disabled={busy}
                    onClick={() => {
                      setCredentialFor(p.name);
                      setKey("");
                    }}
                  >
                    {t("Set session credential")}
                  </Button>
                )}
                <Button
                  size="sm"
                  icon={p.enabled ? Unplug : Plug}
                  disabled={busy}
                  onClick={() =>
                    void action(p.name, p.enabled ? "disconnect" : "activate")
                  }
                >
                  {t(p.enabled ? "Disconnect" : "Reconnect")}
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  className="ag-danger"
                  icon={Trash2}
                  disabled={busy}
                  onClick={() => void remove(p.name)}
                >
                  {t("Remove configuration")}
                </Button>
              </Actions>
              {(p.kind === "ollama" || p.kind === "openai-local") && (
                <OllamaModels connection={p} refresh={refresh} />
              )}
              {credentialFor === p.name && (
                <form
                  className="ag-form"
                  onSubmit={(e) => {
                    e.preventDefault();
                    void action(p.name, "session", { key });
                  }}
                >
                  <Field
                    label={t("Session API key")}
                    hint={t(
                      "Held in server memory only; cleared on disconnect or restart.",
                    )}
                    required
                  >
                    <Input
                      type="password"
                      autoComplete="off"
                      value={key}
                      onChange={(e) => setKey(e.target.value)}
                    />
                  </Field>
                  <Actions>
                    <Button
                      type="submit"
                      size="sm"
                      variant="primary"
                      loading={busy}
                    >
                      {t("Save session credential")}
                    </Button>
                    <Button
                      size="sm"
                      onClick={() => {
                        setCredentialFor(null);
                        setKey("");
                      }}
                    >
                      {t("Cancel")}
                    </Button>
                  </Actions>
                </form>
              )}
            </Card>
          );
        })}
        <Card padding="compact" className="ag-conn">
          <ConnectionHead
            icon={Server}
            title={t("External Agent")}
            status={<Badge tone={ext.tone}>{t(ext.text)}</Badge>}
          />
          <Hint>
            External credentials and dataset scopes are server-managed. HF login
            does not change these permissions.
          </Hint>
          {external?.datasets?.length ? (
            <p className="ag-endpoint">{external.datasets.join(" · ")}</p>
          ) : (
            <p className="ag-muted">
              {t("No external dataset scope configured.")}
            </p>
          )}
          {external?.grants?.map((grant) => (
            <p key={grant.id} className="ag-muted">
              {t("Scoped connection")} · {grant.calls} {t("calls")} ·{" "}
              {grant.expires_in_seconds == null
                ? t("no expiry — until you disconnect")
                : `${t("expires in")} ${Math.max(1, Math.round(grant.expires_in_seconds / 3600))}h`}
              {grant.idle_seconds != null &&
                ` · ${t("last call")} ${
                  grant.idle_seconds < 60
                    ? t("just now")
                    : `${Math.round(grant.idle_seconds / 60)}${t("m ago")}`
                }`}
            </p>
          ))}
          <Actions>
            <GatedButton
              size="sm"
              icon={external?.enabled ? Unplug : Plug}
              disabled={busy}
              reason={
                !external?.configured
                  ? t("No external connection is configured on the server yet.")
                  : null
              }
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
            </GatedButton>
          </Actions>
        </Card>
      </section>
    </T>
  );
}
