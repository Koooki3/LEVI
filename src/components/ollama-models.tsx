"use client";

import { useEffect, useId, useState } from "react";
import type { Connection } from "./agent-connections";
import { Download as DownloadIcon, X } from "lucide-react";
import { T, useLocale } from "./levi-locale";
import { Badge, Button, Checkbox, Progress, Tag } from "@/components/ds";
import { RequestProblem } from "@/components/pages-ui/feedback";
import { Actions, Disclosure, GatedButton, Hint } from "./agent-ui";

type Inventory = {
  version: string;
  model: {
    name: string;
    digest: string;
    size: number;
    // A local OpenAI-compatible server: the weights it serves and its context.
    root?: string | null;
    max_model_len?: number | null;
  } | null;
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
  // A local OpenAI-compatible server (vLLM) is inspected and bound the same
  // way, but it loads its own weights and keeps them resident while it runs:
  // no download and no memory management here.
  const server = connection.kind === "openai-local";
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
    if (server) return;
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
  }, [connection.name, server]);
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
  const consentFirst = t("Tick the authorization above first.");
  const memoryWhy = useId();
  const memoryReason = !connection.enabled
    ? t("Reconnect this model first.")
    : !memoryConsent
      ? consentFirst
      : null;
  return (
    <T>
      <section
        className="ag-subcard"
        aria-label={t(server ? "Local server model" : "Local Ollama models")}
      >
        <h4>{t(server ? "Local server model" : "Local Ollama models")}</h4>
        {server ? (
          <Hint>
            External server: its weights, context and GPU memory belong to the
            process that serves it. LEVI inspects and binds what it serves; it
            never downloads, loads or stops it.
          </Hint>
        ) : (
          <Hint>
            External service: model storage and process lifecycle are managed by
            Ollama. LEVI does not stop this service or guess its model
            directory.
          </Hint>
        )}
        <Hint>
          No model inference or automatic download occurs when opening this
          panel.
        </Hint>
        {error && (
          <RequestProblem
            action="The local model request failed"
            message={error}
          />
        )}
        <Actions>
          <GatedButton
            size="sm"
            disabled={busy}
            reason={
              !connection.enabled
                ? t("Reconnect this model before you inspect it.")
                : null
            }
            onClick={() =>
              void action(async () => {
                setInventory(await request<Inventory>(path));
              })
            }
          >
            {t("Inspect local models")}
          </GatedButton>
        </Actions>
        {inventory && (
          <div className="ag-stack">
            <p>
              {server ? t("Server") : "Ollama"} {inventory.version} ·{" "}
              <Badge tone={inventory.model ? "success" : "warning"}>
                {t(
                  inventory.model
                    ? server
                      ? "Model served"
                      : "Model installed"
                    : server
                      ? "Model not served"
                      : "Model not installed",
                )}
              </Badge>
            </p>
            {inventory.model && (
              <>
                {server ? (
                  <>
                    <p>
                      {inventory.model.name} · {t("Served context")}{" "}
                      {inventory.model.max_model_len ?? t("Unknown")}
                    </p>
                    {inventory.model.root && (
                      <p className="ag-endpoint">{inventory.model.root}</p>
                    )}
                  </>
                ) : (
                  <p>
                    {inventory.model.name} ·{" "}
                    {(inventory.model.size / 1024 ** 3).toFixed(2)} GiB
                  </p>
                )}
                <p className="ag-endpoint">{inventory.model.digest}</p>
                {server ? (
                  <p>
                    The server declares no capabilities; confirm them for this
                    model.
                  </p>
                ) : (
                  <p>
                    {t("Service-declared capabilities")} ·{" "}
                    {inventory.capabilities.length
                      ? inventory.capabilities.map((c) => (
                          <Tag key={c}>{c}</Tag>
                        ))
                      : t("Unknown")}
                  </p>
                )}
                <Hint>
                  Declared capabilities are not a quality evaluation. Binding a
                  new digest requires a new task plan.
                </Hint>
                <Checkbox
                  label={t(
                    "I confirm this model supports structured JSON output",
                  )}
                  checked={structured}
                  onChange={(e) => setStructured(e.target.checked)}
                />
                <Checkbox
                  label={t("Model supports image input")}
                  checked={vision}
                  disabled={
                    !server && !inventory.capabilities.includes("vision")
                  }
                  onChange={(e) => setVision(e.target.checked)}
                />
                <Actions>
                  <GatedButton
                    size="sm"
                    disabled={busy}
                    reason={
                      !structured
                        ? t("Confirm structured JSON output above first.")
                        : null
                    }
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
                      server
                        ? inventory.digest_matches
                          ? "Rebind served model"
                          : "Bind served model"
                        : inventory.digest_matches
                          ? "Rebind installed model"
                          : "Bind installed model",
                    )}
                  </GatedButton>
                </Actions>
              </>
            )}
          </div>
        )}
        {connection.model_digest && !server && (
          <div className="ag-stack">
            <Hint>
              This request may initialize model hardware. It will not download a
              missing model.
            </Hint>
            <Checkbox
              label={t("I authorize model memory management")}
              checked={memoryConsent}
              onChange={(e) => setMemoryConsent(e.target.checked)}
            />
            {memoryReason && <Hint id={memoryWhy}>{memoryReason}</Hint>}
            <Actions>
              {(["load", "unload"] as const).map((operation) => (
                <Button
                  size="sm"
                  key={operation}
                  disabled={busy || !!memoryReason}
                  aria-describedby={memoryReason ? memoryWhy : undefined}
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
                </Button>
              ))}
            </Actions>
            {memoryDone && (
              <p role="status">{t("Model memory operation completed")}</p>
            )}
          </div>
        )}
        {!server && (
          <Disclosure
            summary={t("Download model explicitly")}
            icon={DownloadIcon}
          >
            <p className="ag-endpoint">
              {connection.model} · {connection.base_url}
            </p>
            <Hint>
              The Ollama service will access its model registry. Review the
              model license and available disk space first. Download size is not
              GPU memory usage.
            </Hint>
            <Checkbox
              label={t(
                "I authorize this model download and have reviewed its license",
              )}
              checked={confirmed}
              onChange={(e) => setConfirmed(e.target.checked)}
            />
            <Actions>
              <GatedButton
                size="sm"
                disabled={busy}
                reason={
                  active
                    ? t("A download is already running.")
                    : !connection.enabled
                      ? t("Reconnect this model first.")
                      : !confirmed
                        ? consentFirst
                        : null
                }
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
                {t("Start model download")}
              </GatedButton>
            </Actions>
          </Disclosure>
        )}
        {downloads.map((job) => (
          <div
            key={job.id}
            className="ag-download"
            role="group"
            aria-label={job.id}
          >
            <p>
              {job.model} · {t(job.status)}{" "}
              {job.cancel_requested ? t("Cancellation requested") : ""}
            </p>
            {job.progress?.total != null && job.progress.total > 0 ? (
              <>
                <Progress
                  label={t("Current model layer download")}
                  max={job.progress.total}
                  value={job.progress.completed ?? 0}
                />
                <p className="ag-muted">
                  {((job.progress.completed ?? 0) / 1024 ** 2).toFixed(1)} /{" "}
                  {(job.progress.total / 1024 ** 2).toFixed(1)} MiB
                </p>
              </>
            ) : (
              <p>{job.progress?.status ?? t("Waiting for progress")}</p>
            )}
            {job.error_code && (
              <RequestProblem
                action="The model download failed"
                message={job.error_code}
              />
            )}
            {["queued", "running"].includes(job.status) && (
              <Actions>
                <Button
                  size="sm"
                  icon={X}
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
                  {t("Cancel download")}
                </Button>
              </Actions>
            )}
          </div>
        ))}
        {!server && (
          <Hint>
            Cancellation closes LEVI&apos;s download stream; a download shared
            with another Ollama client may continue.
          </Hint>
        )}
      </section>
    </T>
  );
}
