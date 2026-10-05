"use client";

import { useState } from "react";
import { Server } from "lucide-react";
import { T, useLocale } from "./levi-locale";
import { Badge, Button, Card, Checkbox, Field, Input } from "@/components/ds";
import { RequestProblem } from "@/components/pages-ui/feedback";
import { Actions, ConnectionHead, GatedButton, Hint } from "./agent-ui";

type Runtime = {
  installed: boolean;
  running: boolean;
  url: string;
  models_path: string;
  log_path: string;
  stop_requested?: boolean;
};

export default function OllamaRuntime({
  configured,
  connectionExists,
}: {
  configured: () => Promise<void>;
  connectionExists: boolean;
}) {
  const { t } = useLocale();
  const [status, setStatus] = useState<Runtime | null>(null);
  const [port, setPort] = useState(11435);
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function action(operation: "status" | "start" | "stop" | "use") {
    setBusy(true);
    setError("");
    try {
      const response = await fetch(
        operation === "use"
          ? "/api/levi/agent/v1/providers"
          : `/api/levi/agent/v1/ollama/runtime${operation === "status" ? "" : "/" + operation}`,
        {
          method: operation === "status" ? "GET" : "POST",
          headers: { "Content-Type": "application/json" },
          body:
            operation === "start"
              ? JSON.stringify({ port, approve_start: true })
              : operation === "use"
                ? JSON.stringify({
                    name: "ollama-managed",
                    kind: "ollama",
                    base_url: status?.url,
                    model: "qwen3.5:4b",
                    allow_localhost: true,
                    structured_output: true,
                  })
                : undefined,
        },
      );
      const result = await response.json();
      if (!response.ok)
        throw new Error(
          typeof result.detail === "string"
            ? result.detail
            : "Local model request failed",
        );
      if (operation === "use") await configured();
      else setStatus(result);
      setConsent(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }
  const needsConsent = t("Tick the authorization above first.");
  return (
    <T>
      <Card padding="compact" className="ag-conn">
        <ConnectionHead
          icon={Server}
          title={t("LEVI-owned Ollama service")}
          status={
            status ? (
              <Badge
                tone={
                  status.stop_requested
                    ? "warning"
                    : status.running
                      ? "success"
                      : "neutral"
                }
              >
                {t(
                  status.stop_requested
                    ? "Stop requested"
                    : status.running
                      ? "Service process running"
                      : "Service stopped",
                )}
              </Badge>
            ) : undefined
          }
        />
        <Hint>
          Optional dedicated instance. Requires an explicitly installed Ollama
          executable. Starting it may initialize hardware; it does not download
          or run a model.
        </Hint>
        <Hint>
          Cloud features are disabled. This is not operating-system network
          isolation.
        </Hint>
        {error && (
          <RequestProblem
            action="The Ollama service request failed"
            message={error}
          />
        )}
        <Actions>
          <Button
            size="sm"
            disabled={busy}
            onClick={() => void action("status")}
          >
            {t("Check runtime installation")}
          </Button>
        </Actions>
        {status && (
          <div className="ag-stack">
            <p className="ag-muted">
              {t(
                status.installed
                  ? "Runtime installed"
                  : "Ollama executable not found",
              )}{" "}
              · {status.url}
            </p>
            <p className="ag-endpoint">
              {t("Model storage")} · {status.models_path}
            </p>
            <p className="ag-endpoint">
              {t("Service log")} · {status.log_path}
            </p>
            <Field label={t("Dedicated port")}>
              <Input
                type="number"
                min={1024}
                max={65535}
                value={port}
                disabled={status.running}
                onChange={(e) => setPort(Number(e.target.value))}
              />
            </Field>
            <Checkbox
              label={t("I authorize this owned service operation")}
              checked={consent}
              onChange={(e) => setConsent(e.target.checked)}
            />
            <Actions>
              <GatedButton
                size="sm"
                disabled={busy}
                reason={
                  !status.installed
                    ? t("The Ollama executable was not found on this machine.")
                    : status.running
                      ? t("The service is already running.")
                      : !consent
                        ? needsConsent
                        : null
                }
                onClick={() => void action("start")}
              >
                {t("Start owned service")}
              </GatedButton>
              <GatedButton
                size="sm"
                disabled={busy}
                reason={
                  !status.running
                    ? t("The service is not running.")
                    : !consent
                      ? needsConsent
                      : null
                }
                onClick={() => void action("stop")}
              >
                {t("Stop owned service")}
              </GatedButton>
              <GatedButton
                size="sm"
                disabled={busy}
                reason={
                  !status.running
                    ? t("The service is not running.")
                    : connectionExists
                      ? t("The Qwen connection is already configured.")
                      : !consent
                        ? needsConsent
                        : null
                }
                onClick={() => void action("use")}
              >
                {t("Configure Qwen connection")}
              </GatedButton>
            </Actions>
          </div>
        )}
      </Card>
    </T>
  );
}
