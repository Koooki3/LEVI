"use client";

import { useEffect, useState } from "react";
import type { Connection } from "./agent-connections";
import { T, useLocale } from "./levi-locale";

type Inventory = {
  version: string;
  model: { name: string; digest: string; size: number } | null;
  capabilities: string[];
  digest_matches: boolean;
};
type Download = {
  id: string;
  provider: string;
  model: string;
  status: string;
  cancel_requested: boolean;
  progress: {
    status: string;
    completed?: number;
    total?: number;
    digest?: string;
  } | null;
  error_code?: string;
};
const base = "/api/levi/agent/v1";
async function request<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(base + path, {
    method: body === undefined ? "GET" : "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok)
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : "Local model request failed",
    );
  return data;
}

export default function OllamaModels({
  connection,
  refresh,
}: {
  connection: Connection;
  refresh: () => Promise<void>;
}) {
  const { t } = useLocale();
  const [inventory, setInventory] = useState<Inventory | null>(null);
  const [downloads, setDownloads] = useState<Download[]>([]);
  const [confirmed, setConfirmed] = useState(false);
  const [structured, setStructured] = useState(false);
  const [vision, setVision] = useState(connection.vision);
  const [busy, setBusy] = useState(false);
  const [memoryConsent, setMemoryConsent] = useState(false);
  const [memoryDone, setMemoryDone] = useState(false);
  const [error, setError] = useState("");
  const path = `/providers/${encodeURIComponent(connection.name)}/ollama`;
  useEffect(() => {
    setInventory(null);
    setConfirmed(false);
    setStructured(false);
  }, [connection.base_url, connection.model]);
  useEffect(() => {
    let disposed = false;
    const load = () =>
      request<Download[]>("/model-downloads")
        .then((rows) => {
          if (!disposed)
            setDownloads(
              rows.filter((row) => row.provider === connection.name),
            );
        })
        .catch(() => {});
    void load();
    const timer = setInterval(load, 2000);
    return () => {
      disposed = true;
      clearInterval(timer);
    };
  }, [connection.name]);
  async function action(fn: () => Promise<void>) {
    setBusy(true);
    setError("");
    try {
      await fn();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }
  const active = downloads.some((item) =>
    ["running", "queued"].includes(item.status),
  );
  return (
    <T>
      <section
        className="levi-connection-card"
        aria-label={t("Local Ollama models")}
      >
        <h4>Local Ollama models</h4>
        <p className="levi-agent-muted">
          External service: model storage and process lifecycle are managed by
          Ollama. LEVI does not stop this service or guess its model directory.
        </p>
        <p className="levi-agent-muted">
          No model inference or automatic download occurs when opening this
          panel.
        </p>
        {error && (
          <p className="levi-agent-error" role="alert">
            {t(error)}
          </p>
        )}
        <button
          disabled={busy || !connection.enabled}
          onClick={() =>
            void action(async () => {
              setInventory(await request<Inventory>(path));
            })
          }
        >
          Inspect local models
        </button>
        {inventory && (
          <div>
            <p>
              Ollama {inventory.version} ·{" "}
              {t(inventory.model ? "Model installed" : "Model not installed")}
            </p>
            {inventory.model && (
              <>
                <p>
                  {inventory.model.name} ·{" "}
                  {(inventory.model.size / 1024 ** 3).toFixed(2)} GiB
                </p>
                <p className="levi-agent-endpoint">{inventory.model.digest}</p>
                <p>
                  {t("Service-declared capabilities")} ·{" "}
                  {inventory.capabilities.join(", ") || t("Unknown")}
                </p>
                <p className="levi-agent-muted">
                  Declared capabilities are not a quality evaluation. Binding a
                  new digest requires a new task plan.
                </p>
                <label className="levi-agent-check">
                  <input
                    type="checkbox"
                    checked={structured}
                    onChange={(e) => setStructured(e.target.checked)}
                  />
                  I confirm this model supports structured JSON output
                </label>
                <label className="levi-agent-check">
                  <input
                    type="checkbox"
                    checked={vision}
                    disabled={!inventory.capabilities.includes("vision")}
                    onChange={(e) => setVision(e.target.checked)}
                  />
                  Model supports image input
                </label>
                <button
                  disabled={busy || !structured}
                  onClick={() =>
                    void action(async () => {
                      await request(path + "/bind", {
                        digest: inventory.model!.digest,
                        vision,
                        structured_output: true,
                      });
                      await refresh();
                      setInventory(await request<Inventory>(path));
                    })
                  }
                >
                  {t(
                    inventory.digest_matches
                      ? "Rebind installed model"
                      : "Bind installed model",
                  )}
                </button>
              </>
            )}
          </div>
        )}
        {connection.model_digest && (
          <div>
            <p className="levi-agent-muted">
              This request may initialize model hardware. It will not download a
              missing model.
            </p>
            <label className="levi-agent-check">
              <input
                type="checkbox"
                checked={memoryConsent}
                onChange={(e) => setMemoryConsent(e.target.checked)}
              />
              I authorize model memory management
            </label>
            {(["load", "unload"] as const).map((operation) => (
              <button
                key={operation}
                disabled={busy || !connection.enabled || !memoryConsent}
                onClick={() =>
                  void action(async () => {
                    await request(path + "/memory", {
                      operation,
                      approve_hardware_use: true,
                    });
                    setMemoryConsent(false);
                    setMemoryDone(true);
                  })
                }
              >
                {t(
                  operation === "load"
                    ? "Load bound model"
                    : "Unload bound model",
                )}
              </button>
            ))}
            {memoryDone && (
              <p role="status">Model memory operation completed</p>
            )}
          </div>
        )}
        <details>
          <summary>Download model explicitly</summary>
          <p>
            {connection.model} · {connection.base_url}
          </p>
          <p className="levi-agent-muted">
            The Ollama service will access its model registry. Review the model
            license and available disk space first. Download size is not GPU
            memory usage.
          </p>
          <label className="levi-agent-check">
            <input
              type="checkbox"
              checked={confirmed}
              onChange={(e) => setConfirmed(e.target.checked)}
            />
            I authorize this model download and have reviewed its license
          </label>
          <button
            disabled={busy || active || !confirmed || !connection.enabled}
            onClick={() =>
              void action(async () => {
                const job = await request<Download>(path + "/download", {
                  request_id: crypto.randomUUID(),
                  approve_download: true,
                });
                setDownloads((rows) => [...rows, job]);
                setConfirmed(false);
              })
            }
          >
            Start model download
          </button>
        </details>
        {downloads.map((job) => (
          <article key={job.id} aria-label={job.id}>
            <p>
              {job.model} · {t(job.status)}{" "}
              {job.cancel_requested ? t("Cancellation requested") : ""}
            </p>
            {job.progress?.total != null && job.progress.total > 0 ? (
              <>
                <progress
                  aria-label={t("Current model layer download")}
                  max={job.progress.total}
                  value={job.progress.completed ?? 0}
                />
                <p>
                  {t("Current model layer download")} ·{" "}
                  {((job.progress.completed ?? 0) / 1024 ** 2).toFixed(1)} /{" "}
                  {(job.progress.total / 1024 ** 2).toFixed(1)} MiB
                </p>
              </>
            ) : (
              <p>{job.progress?.status ?? t("Waiting for progress")}</p>
            )}
            {job.error_code && <p role="alert">{t(job.error_code)}</p>}
            {["queued", "running"].includes(job.status) && (
              <button
                disabled={busy || job.cancel_requested}
                onClick={() =>
                  void action(async () => {
                    const updated = await request<Download>(
                      `/model-downloads/${encodeURIComponent(job.id)}/cancel`,
                      {},
                    );
                    setDownloads((rows) =>
                      rows.map((row) =>
                        row.id === updated.id ? updated : row,
                      ),
                    );
                  })
                }
              >
                Cancel download
              </button>
            )}
          </article>
        ))}
        <p className="levi-agent-muted">
          Cancellation closes LEVI&apos;s download stream; a download shared
          with another Ollama client may continue.
        </p>
      </section>
    </T>
  );
}
