"use client";

import { useEffect, useState } from "react";
import { usePathname } from "next/navigation";
import AgentRuntimeConnections from "./agent-runtime-connections";
import AgentPilot from "./agent-pilot";
import AgentQuality from "./agent-quality";
import AgentDefinitions from "./agent-definitions";
import AgentPlan, { type HarnessPlan } from "./agent-plan";
import AgentObjectTool from "./agent-object-tool";
import AgentReviewQueue from "./agent-review-queue";
import AgentConnections, { type Connection } from "./agent-connections";
import {
  readBrowserStorage,
  writeBrowserStorage,
} from "@/utils/browserStorage";
import { T, useLocale } from "./levi-locale";

const base = "/api/levi/agent/v1";
async function api<T>(path: string, value?: unknown): Promise<T> {
  const response = await fetch(
    base + path,
    value === undefined
      ? { cache: "no-store" }
      : {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(value),
        },
  );
  const data = await response.json();
  if (!response.ok)
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : JSON.stringify(data.detail),
    );
  return data;
}
function tool<T>(name: string, args: unknown, key?: string) {
  return api<T>("/tools", { name, arguments: args, idempotency_key: key });
}
type Provider = Connection;
type Run = {
  plan?: HarnessPlan;
  cache_hits?: number;
  id: string;
  status: string;
  context: {
    repo_id: string;
    episodes: number[];
    cameras: string[];
    instruction: string;
    provider: string;
    pilot_runtime?: "codex" | "claude";
    workflow?: { kind: string };
    budget?: unknown;
    allow_media_egress?: boolean;
  };
  completed: number[];
  snapshot_bytes: number;
  requests: number;
  tokens: number;
  reserved_tokens: number;
  reason?: string;
  changes?: string;
};
type Proposal = {
  episode_index: number;
  kind: string;
  content: string;
  start: number;
  end: number | null;
  evidence_ids: string[];
};
type Change = {
  object_jobs: string[];
  undo_of?: string;
  decisions: Record<string, string>;
  id: string;
  revision: number;
  status: string;
  proposals: Proposal[];
  base_revision: string;
  provenance: { coverage: string };
};
type Evidence = {
  id: string;
  episode_index: number;
  camera_key?: string;
  timestamp: number;
  frame_index: number;
  artifact?: string;
};

export default function AgentWorkbench() {
  const { t } = useLocale();
  const pathname = usePathname();
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState<"task" | "settings">("task");
  const [providers, setProviders] = useState<Provider[]>([]);
  const [provider, setProvider] = useState("");
  const [runs, setRuns] = useState<Run[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  useEffect(() => {
    const id = new URLSearchParams(window.location.search).get("agent_run");
    if (id) {
      setSelected(id);
      setOpen(true);
    }
  }, []);
  const run = runs.find((r) => r.id === selected);
  const [change, setChange] = useState<Change | null>(null);
  const [evidence, setEvidence] = useState<Evidence[]>([]);
  const [repo, setRepo] = useState("");
  const [episodes, setEpisodes] = useState("0");
  const [cameras, setCameras] = useState("");
  const [instruction, setInstruction] = useState("");
  const [workflow, setWorkflow] = useState("review");
  const [definitions, setDefinitions] = useState("[]");
  const [concepts, setConcepts] = useState("");
  const [step, setStep] = useState(2);
  const [frameCap, setFrameCap] = useState(96);
  const [clarifications, setClarifications] = useState<string[]>([]);
  const [egress, setEgress] = useState(false);
  const [mode, setMode] = useState("draft");
  const [maxCalls, setMaxCalls] = useState(8);
  const [maxTokens, setMaxTokens] = useState(16000);
  const [storageMiB, setStorageMiB] = useState(512);
  const [name, setName] = useState("model");
  const [url, setUrl] = useState("");
  const [model, setModel] = useState("");
  const [keyEnv, setKeyEnv] = useState("LEVI_MODEL_API_KEY");
  const [vision, setVision] = useState(false);
  const [supportsTools, setSupportsTools] = useState(false);
  const [allowLocal, setAllowLocal] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [draftEdited, setDraftEdited] = useState(false);
  const [pilotRuntime, setPilotRuntime] = useState<"" | "codex" | "claude">("");
  const [follow, setFollow] = useState(false);
  const [refreshVersion, setRefreshVersion] = useState(0);
  const [activeEvidence, setActiveEvidence] = useState<Evidence | null>(null);

  useEffect(() => {
    const saved = readBrowserStorage("local", "levi-agent-provider");
    if (saved) setProvider(saved);
  }, []);
  useEffect(() => {
    if (provider) writeBrowserStorage("local", "levi-agent-provider", provider);
  }, [provider]);
  useEffect(() => {
    const toggle = () => setOpen((value) => !value);
    const connections = () => {
      setTab("settings");
      setOpen(true);
    };
    window.addEventListener("levi-agent-connections", connections);
    window.addEventListener("levi-agent-toggle", toggle);
    return () => {
      window.removeEventListener("levi-agent-toggle", toggle);
      window.removeEventListener("levi-agent-connections", connections);
    };
  }, []);
  useEffect(() => {
    const parts = pathname.split("/").filter(Boolean);
    const episodeMatch = parts[2]?.match(/^(?:episode_)?(\d+)$/);
    if (parts.length === 3 && episodeMatch) {
      setRepo(decodeURIComponent(parts.slice(0, 2).join("/")));
      setEpisodes(episodeMatch[1]);
    }
  }, [pathname]);
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    const refresh = async () => {
      try {
        const [list, settings] = await Promise.all([
          api<Run[]>("/runs"),
          api<Provider[]>("/providers"),
        ]);
        if (!cancelled) {
          setRuns(list);
          setProviders(settings);
        }
      } catch (e) {
        if (!cancelled) setError(String(e));
      }
    };
    void refresh();
    const timer = setInterval(refresh, 2500);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [open]);
  useEffect(() => {
    if (
      providers.length &&
      !["external", "local-tools"].includes(provider) &&
      !providers.some((p) => p.name === provider)
    )
      setProvider(providers[0].name);
  }, [providers, provider]);
  useEffect(() => {
    if (!run?.changes || draftEdited) return;
    let cancelled = false;
    const refresh = () =>
      Promise.all([
        tool<Change>("changes.diff", { changeset_id: run.changes }),
        tool<{ items: Evidence[] }>("media.sample", { run_id: run.id }),
      ])
        .then(([next, media]) => {
          if (!cancelled) {
            setChange(next);
            setEvidence(media.items);
          }
        })
        .catch((e) => setError(String(e)));
    void refresh();
    const timer = setInterval(refresh, 2500);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [run?.changes, run?.status, run?.id, draftEdited, refreshVersion]);

  async function act(fn: () => Promise<void>) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await fn();
      setRuns(await api<Run[]>("/runs"));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }
  const context = () => ({
    workflow: {
      kind: workflow,
      definitions: JSON.parse(definitions),
      object_concepts: concepts
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean),
      coarse_step_seconds: step,
      max_evidence_frames: frameCap,
    },
    repo_id: repo,
    episodes: episodes.split(",").map((v) => Number(v.trim())),
    cameras: cameras
      .split(",")
      .map((v) => v.trim())
      .filter(Boolean),
    instruction,
    provider: pilotRuntime ? "external" : provider,
    pilot_runtime: pilotRuntime || null,
    mode,
    allow_media_egress: egress,
    budget: {
      max_calls: maxCalls,
      max_tokens: maxTokens,
      max_seconds: 300,
      max_snapshot_bytes: storageMiB * 1024 * 1024,
    },
    samples_per_episode: 3,
  });
  function seek(row: Evidence) {
    if (!run) return;
    setActiveEvidence(row);
    // No navigation/unmount: unsaved annotation edits remain intact.
    window.dispatchEvent(
      new CustomEvent("levi-agent-seek", {
        detail: {
          repo_id: run.context.repo_id,
          episode_index: row.episode_index,
          timestamp: row.timestamp,
          camera_key: row.camera_key,
          follow,
        },
      }),
    );
    setNotice(
      "Evidence navigation stays in the current episode. Open other episodes manually after saving your edits.",
    );
  }
  if (!open) return null;
  return (
    <T>
      <aside className="levi-agent-dock" aria-label="Agent Workbench">
        <header>
          <div>
            <small>LEVI / AGENT LAB</small>
            <h2>Agent Workbench</h2>
          </div>
          <button
            aria-label="Close Agent Workbench"
            onClick={() => setOpen(false)}
          >
            ×
          </button>
        </header>
        <p className="levi-agent-muted">
          Experimental · sampled evidence · human review required
        </p>
        <nav className="levi-agent-tabs">
          <button aria-pressed={tab === "task"} onClick={() => setTab("task")}>
            Tasks & review
          </button>
          <button
            aria-pressed={tab === "settings"}
            onClick={() => setTab("settings")}
          >
            Model settings
          </button>
        </nav>
        {error && (
          <p role="alert" className="levi-agent-error">
            {error}
          </p>
        )}
        {notice && <p role="status">{t(notice)}</p>}
        {tab === "settings" ? (
          <>
            <label>
              {t("Execution channel")}
              <select
                value={pilotRuntime}
                onChange={(e) => {
                  setPilotRuntime(e.target.value as "" | "codex" | "claude");
                  if (e.target.value) setProvider("external");
                }}
              >
                <option value="">{t("API / external MCP")}</option>
                <option value="codex">Codex Pilot</option>
                <option value="claude">Claude Code Pilot</option>
              </select>
            </label>
            <AgentRuntimeConnections />
            <AgentConnections
              providers={providers}
              selected={provider}
              select={setProvider}
              refresh={async () =>
                setProviders(await api<Provider[]>("/providers"))
              }
              edit={(p) => {
                setName(p.name);
                setUrl(p.base_url);
                setModel(p.model);
                setKeyEnv(p.key_env);
                setVision(p.vision);
                setSupportsTools(p.tools);
                setAllowLocal(p.allow_localhost);
              }}
            />
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void act(async () => {
                  await api("/providers", {
                    name,
                    base_url: url,
                    model,
                    key_env: keyEnv,
                    vision,
                    tools: supportsTools,
                    structured_output: false,
                    allow_localhost: allowLocal,
                  });
                  setProviders(await api<Provider[]>("/providers"));
                  setProvider(name);
                  setNotice("Provider saved. Credentials stay on the server.");
                });
              }}
            >
              <label>
                Provider name
                <input
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  required
                />
              </label>
              <label>
                Compatible API base URL
                <input
                  type="url"
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                  placeholder="https://provider.example/v1"
                  required
                />
              </label>
              <label>
                Model ID
                <input
                  value={model}
                  onChange={(e) => setModel(e.target.value)}
                  required
                />
              </label>
              <label>
                Server API-key environment variable
                <input
                  value={keyEnv}
                  onChange={(e) => setKeyEnv(e.target.value)}
                  required
                />
              </label>
              <p className="levi-agent-muted">
                Set this environment variable before starting LEVI. Never paste
                the key here. Tool-call support is required.
              </p>
              <label className="levi-agent-check">
                <input
                  type="checkbox"
                  checked={supportsTools}
                  onChange={(e) => setSupportsTools(e.target.checked)}
                />
                Model supports structured tool calls
              </label>
              <label className="levi-agent-check">
                <input
                  type="checkbox"
                  checked={vision}
                  onChange={(e) => setVision(e.target.checked)}
                />
                Model supports image input
              </label>
              <label className="levi-agent-check">
                <input
                  type="checkbox"
                  checked={allowLocal}
                  onChange={(e) => setAllowLocal(e.target.checked)}
                />
                Allow an explicitly configured loopback model endpoint
              </label>
              <button disabled={busy}>Save model settings</button>
              <ul>
                {providers.map((p) => (
                  <li key={p.name}>
                    {p.name} ·{" "}
                    <T>
                      {p.credential_ready
                        ? "Credential available"
                        : "Credential missing"}
                    </T>
                  </li>
                ))}
              </ul>
            </form>
          </>
        ) : (
          <>
            <details open={!run}>
              <summary>New task · frozen scope</summary>
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  if (
                    draftEdited &&
                    !window.confirm(t("Discard unsaved draft edits?"))
                  )
                    return;
                  void act(async () => {
                    const request = context();
                    const check = await tool<{
                      ready: boolean;
                      questions: { message: string }[];
                    }>("plans.clarify", request);
                    setClarifications(check.questions.map((q) => q.message));
                    if (!check.ready) return;
                    const next = await tool<Run>("runs.plan", request);
                    setSelected(next.id);
                    setChange(null);
                    setEvidence([]);
                    setActiveEvidence(null);
                    setDraftEdited(false);
                  });
                }}
              >
                <label>
                  Agent model
                  <select
                    value={provider}
                    onChange={(e) => setProvider(e.target.value)}
                    required
                  >
                    <option value="">
                      {t("Choose a configured provider")}
                    </option>
                    {workflow === "objects" && (
                      <option value="local-tools">
                        {t("Local vision tools (no model)")}
                      </option>
                    )}
                    <option value="external">
                      {t("External Agent / MCP")}
                    </option>
                    {providers.map((p) => (
                      <option key={p.name} value={p.name}>
                        {p.name}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  Dataset ID
                  <input
                    value={repo}
                    onChange={(e) => setRepo(e.target.value)}
                    placeholder="local/dataset or org/dataset"
                    required
                  />
                </label>
                <label>
                  Episode indices (comma-separated)
                  <input
                    value={episodes}
                    onChange={(e) => setEpisodes(e.target.value)}
                    required
                    pattern="[0-9, ]+"
                  />
                </label>
                <label>
                  Camera keys (comma-separated)
                  <input
                    value={cameras}
                    onChange={(e) => setCameras(e.target.value)}
                    placeholder="observation.images.front"
                  />
                </label>
                <label>
                  Task instructions
                  <textarea
                    value={instruction}
                    onChange={(e) => setInstruction(e.target.value)}
                    required
                    rows={3}
                  />
                </label>
                <label>
                  Annotation workflow
                  <select
                    value={workflow}
                    onChange={(e) => setWorkflow(e.target.value)}
                  >
                    <option value="review">{t("Dataset review")}</option>
                    <option value="temporal">
                      {t("Video subtasks and events")}
                    </option>
                    <option value="objects">{t("Visible object masks")}</option>
                  </select>
                </label>
                {workflow === "temporal" && (
                  <>
                    <p>
                      Define observed start, end and success conditions.
                      Repeated attempts and unknown behavior remain valid.
                    </p>
                    <AgentDefinitions
                      value={definitions}
                      onChange={setDefinitions}
                    />
                    <button
                      type="button"
                      onClick={() =>
                        setDefinitions(
                          JSON.stringify(
                            [
                              {
                                id: "pick",
                                label: "Pick",
                                definition: "Lift the target object",
                                starts_when: "Gripper approaches target",
                                ends_when: "Attempt completes or is abandoned",
                                success_when:
                                  "Object leaves the surface and remains held",
                                confusions:
                                  "Contact alone does not prove success",
                              },
                            ],
                            null,
                            2,
                          ),
                        )
                      }
                    >
                      Insert editable definition example
                    </button>
                    <label>
                      Coarse observation step (seconds)
                      <input
                        type="number"
                        min={0.05}
                        max={60}
                        step={0.05}
                        value={step}
                        onChange={(e) => setStep(Number(e.target.value))}
                      />
                    </label>
                    <label>
                      Maximum evidence frames
                      <input
                        type="number"
                        min={3}
                        max={1000}
                        value={frameCap}
                        onChange={(e) => setFrameCap(Number(e.target.value))}
                      />
                    </label>
                  </>
                )}
                {workflow === "objects" && (
                  <label>
                    Target object concepts
                    <input
                      value={concepts}
                      onChange={(e) => setConcepts(e.target.value)}
                      placeholder="cup, plate"
                    />
                  </label>
                )}
                {clarifications.map((q) => (
                  <p key={q} role="alert">
                    {q}
                  </p>
                ))}
                <label>
                  Operating mode
                  <select
                    value={mode}
                    onChange={(e) => setMode(e.target.value)}
                  >
                    <option value="draft">{t("Draft suggestions")}</option>
                    <option value="read_only">{t("Read only")}</option>
                  </select>
                </label>
                <div className="levi-agent-budget">
                  <label>
                    Call limit
                    <input
                      type="number"
                      min={1}
                      max={1000}
                      value={maxCalls}
                      onChange={(e) => setMaxCalls(Number(e.target.value))}
                    />
                  </label>
                  <label>
                    Token limit
                    <input
                      type="number"
                      min={256}
                      max={1000000}
                      value={maxTokens}
                      onChange={(e) => setMaxTokens(Number(e.target.value))}
                    />
                  </label>
                  <label>
                    Snapshot MiB
                    <input
                      type="number"
                      min={1}
                      value={storageMiB}
                      onChange={(e) => setStorageMiB(Number(e.target.value))}
                    />
                  </label>
                </div>
                <label className="levi-agent-check">
                  <input
                    type="checkbox"
                    checked={egress}
                    onChange={(e) => setEgress(e.target.checked)}
                  />
                  Allow selected evidence to be sent to this model endpoint
                </label>
                <p className="levi-agent-muted">
                  Uses saved annotations only. Unsaved editor changes are never
                  submitted or overwritten automatically.
                </p>
                <button disabled={busy || !provider}>
                  Inspect & create plan
                </button>
              </form>
            </details>
            <label>
              Task center
              <select
                value={selected || ""}
                onChange={(e) => {
                  if (
                    draftEdited &&
                    !window.confirm(t("Discard unsaved draft edits?"))
                  )
                    return;
                  setSelected(e.target.value);
                  setChange(null);
                  setEvidence([]);
                  setActiveEvidence(null);
                  setDraftEdited(false);
                }}
              >
                <option value="">{t("Choose a task")}</option>
                {runs.map((r) => (
                  <option value={r.id} key={r.id}>
                    {r.context.repo_id} · {t(r.status)} · {r.id}
                  </option>
                ))}
              </select>
            </label>
            {run && (
              <section>
                <h3>{run.context.repo_id}</h3>
                <p>
                  <strong>{t(run.status)}</strong> · {run.completed.length}/
                  {run.context.episodes.length} <T>episodes processed</T>
                </p>
                <progress
                  value={run.completed.length}
                  max={run.context.episodes.length}
                />
                <p className="levi-agent-muted">
                  <T>Snapshot MiB</T>:{" "}
                  {(run.snapshot_bytes / 1048576).toFixed(1)} · <T>Calls</T>:{" "}
                  {run.requests} · <T>Tokens</T>: {run.tokens} ·{" "}
                  <T>Reserved tokens</T>: {run.reserved_tokens}
                </p>
                {run.reason && <p role="status">{run.reason}</p>}
                <details>
                  <summary>Approved scope and policy</summary>
                  <pre>
                    {JSON.stringify(
                      {
                        dataset: run.context.repo_id,
                        episodes: run.context.episodes,
                        cameras: run.context.cameras,
                        provider: run.context.provider,
                        workflow: run.context.workflow,
                        budget: run.context.budget,
                        media_egress: run.context.allow_media_egress,
                      },
                      null,
                      2,
                    )}
                  </pre>
                </details>
                {![
                  "running",
                  "queued",
                  "succeeded",
                  "partially_succeeded",
                  "cancelled",
                ].includes(run.status) &&
                  run.plan && (
                    <details>
                      <summary>Revise budget and require new approval</summary>
                      <p>Budget-only revision retains completed work.</p>
                      <label>
                        Call limit
                        <input
                          type="number"
                          min={1}
                          value={maxCalls}
                          onChange={(e) => setMaxCalls(Number(e.target.value))}
                        />
                      </label>
                      <label>
                        Token limit
                        <input
                          type="number"
                          min={256}
                          value={maxTokens}
                          onChange={(e) => setMaxTokens(Number(e.target.value))}
                        />
                      </label>
                      <button
                        disabled={busy}
                        onClick={() =>
                          void act(async () => {
                            await tool("plans.rebudget", {
                              run_id: run.id,
                              revision: run.plan?.revision,
                              budget: context().budget,
                            });
                          })
                        }
                      >
                        Revise budget and require new approval
                      </button>
                    </details>
                  )}
                <AgentPilot
                  runId={run.id}
                  approved={
                    !!run.plan?.approval &&
                    !["succeeded", "cancelled"].includes(run.status)
                  }
                  runtime={run.context.pilot_runtime}
                  repo={run.context.repo_id}
                  edited={draftEdited}
                  follow={follow}
                />
                <AgentPlan
                  plan={run.plan}
                  busy={busy}
                  completed={run.completed}
                  draftRevision={change?.revision}
                  onApprove={() =>
                    void act(async () => {
                      await tool("plans.approve", {
                        run_id: run.id,
                        revision: run.plan?.revision,
                      });
                    })
                  }
                  onPilot={(accepted, note) =>
                    void act(async () => {
                      await tool("plans.review_pilot", {
                        run_id: run.id,
                        revision: change?.revision,
                        accepted,
                        note,
                      });
                    })
                  }
                />
                <p>
                  <T>Cache hits</T>: {run.cache_hits || 0}
                </p>
                <div className="levi-agent-actions">
                  {run.context.workflow?.kind === "objects" && (
                    <button
                      disabled={busy || !run.plan?.approval}
                      onClick={() =>
                        void act(async () => {
                          await tool("runs.prepare", { run_id: run.id });
                        })
                      }
                    >
                      Prepare object evidence without model calls
                    </button>
                  )}
                  {["blocked", "paused", "interrupted", "cancelled"].includes(
                    run.status,
                  ) &&
                    run.completed.length > 0 && (
                      <button
                        disabled={busy}
                        onClick={() =>
                          void act(async () => {
                            await tool("runs.finish", { run_id: run.id });
                          })
                        }
                      >
                        Read completed work
                      </button>
                    )}
                  {[
                    "planned",
                    "paused",
                    "interrupted",
                    "blocked",
                    "waiting_for_review",
                  ].includes(run.status) &&
                    run.context.provider !== "external" &&
                    run.context.workflow?.kind !== "objects" && (
                      <>
                        <button
                          disabled={busy || !run.plan?.approval}
                          onClick={() =>
                            void act(async () => {
                              await tool("runs.execute", {
                                run_id: run.id,
                                pilot: true,
                              });
                            })
                          }
                        >
                          Run pilot
                        </button>
                        <button
                          disabled={busy || !run.plan?.pilot_review?.accepted}
                          onClick={() =>
                            void act(async () => {
                              await tool("runs.resume", {
                                run_id: run.id,
                                pilot: false,
                              });
                            })
                          }
                        >
                          Execute remaining
                        </button>
                      </>
                    )}
                  {["running", "queued"].includes(run.status) && (
                    <button
                      disabled={busy}
                      onClick={() =>
                        void act(async () => {
                          await tool("runs.pause", { run_id: run.id });
                        })
                      }
                    >
                      Pause at boundary
                    </button>
                  )}
                  {!["succeeded", "cancelled"].includes(run.status) && (
                    <button
                      disabled={busy}
                      onClick={() =>
                        void act(async () => {
                          await tool("runs.cancel", { run_id: run.id });
                        })
                      }
                    >
                      Cancel task
                    </button>
                  )}
                </div>
                <label className="levi-agent-check">
                  <input
                    type="checkbox"
                    checked={follow}
                    onChange={(e) => setFollow(e.target.checked)}
                  />
                  Follow evidence in the current episode
                </label>
                {evidence.length > 0 && (
                  <details>
                    <summary>Sampled evidence</summary>
                    <div className="levi-agent-evidence">
                      {evidence.map((row) => (
                        <button key={row.id} onClick={() => seek(row)}>
                          {/* Native image artifacts are authenticated same-origin resources. */}
                          {row.artifact && (
                            // eslint-disable-next-line @next/next/no-img-element
                            <img
                              src={`${base}/runs/${run.id}/artifacts/${row.artifact}`}
                              alt={`${row.camera_key} / ${row.frame_index}`}
                            />
                          )}
                          {row.episode_index} · {row.timestamp.toFixed(3)}s ·{" "}
                          {row.camera_key}
                        </button>
                      ))}
                    </div>
                  </details>
                )}
                <AgentObjectTool
                  key={run.id}
                  runId={run.id}
                  status={run.status}
                  jobIds={change?.object_jobs || []}
                  refresh={() => setRefreshVersion((v) => v + 1)}
                />
                {activeEvidence?.artifact && (
                  <div className="levi-agent-review-evidence">
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img
                      src={`${base}/runs/${run.id}/artifacts/${activeEvidence.artifact}`}
                      alt={`${activeEvidence.camera_key} / ${activeEvidence.frame_index}`}
                    />
                    <p>
                      {activeEvidence.camera_key} ·{" "}
                      {activeEvidence.timestamp.toFixed(3)}s
                    </p>
                  </div>
                )}
                {change && (
                  <section className="levi-agent-review">
                    <h3>Review proposed changes</h3>
                    <AgentQuality runId={run.id} revision={change.revision} />
                    <p>
                      {t(change.status)} · <T>Base revision</T>:{" "}
                      {change.base_revision}
                    </p>
                    <p className="levi-agent-muted">
                      Sparse samples are not full-episode coverage. Verify
                      boundaries and outcomes before approval.
                    </p>
                    {change.undo_of && (
                      <p>
                        Inverse ChangeSet · restores the previous saved
                        annotations.
                      </p>
                    )}
                    {change.status !== "committed" && (
                      <p className="levi-agent-muted">
                        Approval accepts remaining language suggestions;
                        rejected items stay excluded. Object masks require
                        separate decisions.
                      </p>
                    )}
                    <AgentReviewQueue
                      proposals={change.proposals}
                      decisions={change.decisions || {}}
                      disabled={busy || change.status === "committed"}
                      onChange={(proposals) => {
                        setDraftEdited(true);
                        setChange({ ...change, proposals });
                      }}
                      onEvidence={(id) => {
                        const row = evidence.find((e) => e.id === id);
                        if (row) seek(row);
                      }}
                      onDecision={(indices, decision) => {
                        if (draftEdited) {
                          setNotice(
                            "Save draft edits before recording review decisions.",
                          );
                          return;
                        }
                        void act(async () => {
                          setChange(
                            await tool<Change>("changes.review", {
                              changeset_id: change.id,
                              revision: change.revision,
                              indices,
                              decision,
                            }),
                          );
                        });
                      }}
                    />
                    <div className="levi-agent-actions">
                      {change.status === "committed" && (
                        <button
                          disabled={busy}
                          onClick={() =>
                            void act(async () => {
                              const result = await tool<{ output_dir: string }>(
                                "export.run",
                                { run_id: run.id },
                              );
                              setNotice(result.output_dir);
                            })
                          }
                        >
                          Export full dataset with reviewed changes
                        </button>
                      )}
                      {change.status === "committed" && !change.undo_of && (
                        <button
                          disabled={busy}
                          onClick={() =>
                            void act(async () => {
                              const inverse = await tool<
                                Change & { run_id: string }
                              >("changes.undo", { changeset_id: change.id });
                              setSelected(inverse.run_id);
                              setChange(inverse);
                              setDraftEdited(false);
                            })
                          }
                        >
                          Create undo draft
                        </button>
                      )}
                      {draftEdited && (
                        <button
                          disabled={busy}
                          onClick={() =>
                            void act(async () => {
                              const next = await tool<Change>("changes.edit", {
                                changeset_id: change.id,
                                revision: change.revision,
                                proposals: change.proposals,
                              });
                              setChange(next);
                              setDraftEdited(false);
                            })
                          }
                        >
                          Save draft edits
                        </button>
                      )}
                      {!draftEdited && change.status !== "committed" && (
                        <button
                          disabled={busy}
                          onClick={() =>
                            void act(async () => {
                              await tool("changes.validate", {
                                changeset_id: change.id,
                              });
                              setChange(
                                await tool<Change>("changes.approve", {
                                  changeset_id: change.id,
                                  revision: change.revision,
                                }),
                              );
                            })
                          }
                        >
                          Validate & approve
                        </button>
                      )}
                      {!draftEdited && change.status === "approved" && (
                        <button
                          disabled={busy}
                          onClick={() =>
                            void act(async () => {
                              await tool(
                                "changes.commit",
                                {
                                  changeset_id: change.id,
                                  revision: change.revision,
                                },
                                `commit:${change.id}:${change.revision}`,
                              );
                              setChange(
                                await tool<Change>("changes.diff", {
                                  changeset_id: change.id,
                                }),
                              );
                              setNotice(
                                "Committed. Reload saved annotations to see the new revision.",
                              );
                            })
                          }
                        >
                          Commit approved changes
                        </button>
                      )}
                    </div>
                  </section>
                )}
              </section>
            )}
          </>
        )}
      </aside>
    </T>
  );
}
