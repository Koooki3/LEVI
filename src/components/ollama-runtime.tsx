"use client";

import { useState } from "react";
import { T, useLocale } from "./levi-locale";

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
  return (
    <T>
      <article className="levi-connection-card">
        <h3>LEVI-owned Ollama service</h3>
        <p className="levi-agent-muted">
          Optional dedicated instance. Requires an explicitly installed Ollama
          executable. Starting it may initialize hardware; it does not download
          or run a model.
        </p>
        <p className="levi-agent-muted">
          Cloud features are disabled. This is not operating-system network
          isolation.
        </p>
        {error && (
          <p className="levi-agent-error" role="alert">
            {t(error)}
          </p>
        )}
        <button disabled={busy} onClick={() => void action("status")}>
          Check runtime installation
        </button>
        {status && (
          <>
            <p>
              {t(
                status.installed
                  ? "Runtime installed"
                  : "Ollama executable not found",
              )}
            </p>
            <p>
              {t(
                status.stop_requested
                  ? "Stop requested"
                  : status.running
                    ? "Service process running"
                    : "Service stopped",
              )}{" "}
              · {status.url}
            </p>
            <p className="levi-agent-endpoint">
              {t("Model storage")} · {status.models_path}
            </p>
            <p className="levi-agent-endpoint">
              {t("Service log")} · {status.log_path}
            </p>
            <label>
              Dedicated port
              <input
                type="number"
                min={1024}
                max={65535}
                value={port}
                disabled={status.running}
                onChange={(e) => setPort(Number(e.target.value))}
              />
            </label>
            <label className="levi-agent-check">
              <input
                type="checkbox"
                checked={consent}
                onChange={(e) => setConsent(e.target.checked)}
              />
              I authorize this owned service operation
            </label>
            <div className="levi-agent-actions">
              <button
                disabled={
                  busy || !status.installed || status.running || !consent
                }
                onClick={() => void action("start")}
              >
                Start owned service
              </button>
              <button
                disabled={busy || !status.running || !consent}
                onClick={() => void action("stop")}
              >
                Stop owned service
              </button>
              <button
                disabled={
                  busy || !status.running || !consent || connectionExists
                }
                onClick={() => void action("use")}
              >
                Configure Qwen connection
              </button>
            </div>
          </>
        )}
      </article>
    </T>
  );
}
