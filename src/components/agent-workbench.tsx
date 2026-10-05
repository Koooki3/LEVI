"use client";

import {
  PANEL_DEFAULT,
  PANEL_GUTTER,
  PANEL_MIN,
  clampPanelWidth,
  panelAria,
  panelWidthForKey,
} from "./shell/panel-width";
import { useEffect, useState, useRef } from "react";
import { usePathname } from "next/navigation";
import { TeacherChoice, TeachingStatus } from "./agent-supervision";
import AgentRuntimeConnections from "./agent-runtime-connections";
import AgentPilot from "./agent-pilot";
import AgentQuality from "./agent-quality";
import AgentDefinitions from "./agent-definitions";
import AgentPlan, { type HarnessPlan } from "./agent-plan";
import AgentObjectTool from "./agent-object-tool";
import AgentReviewQueue from "./agent-review-queue";
import AgentActivity from "./agent-activity";
import AgentTaskConsole from "./agent-task-console";
import ChipMultiSelect from "./chip-multi-select";
import { useDatasetFacets } from "./dataset-facets";
import AgentConnections, { type Connection } from "./agent-connections";
import {
  readBrowserStorage,
  writeBrowserStorage,
} from "@/utils/browserStorage";
import { T, useLocale } from "./levi-locale";
import { friendlyError } from "./live/friendly-error";
import {
  Badge,
  Button,
  Checkbox,
  Field,
  Input,
  Progress,
  SegmentedControl,
  Select,
  Sheet,
  Tabs,
  Textarea,
  useToast,
} from "./ds";
import {
  Ban,
  Check,
  CircleStop,
  Download,
  FilePlus2,
  Image as ImageIcon,
  ListChecks,
  Pause,
  Play,
  Plus,
  Save,
  ScanSearch,
  ShieldCheck,
  Undo2,
} from "lucide-react";
import {
  HumanActionMark,
  RequestProblem,
} from "@/components/pages-ui/feedback";
import { Actions, Disclosure, GatedButton, Hint } from "./agent-ui";
import { useConfirmAction } from "./shell/confirm";
import { SHELL_EVENTS } from "./shell/shell-events";

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
type ProviderKind = NonNullable<Connection["kind"]>;
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
    supervision?: "none" | "shadow" | "supervised";
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
  const confirm = useConfirmAction();
  // Asked before a task switch would drop staged edits.
  const discardDraft = () =>
    confirm({
      title: t("Discard unsaved draft edits?"),
      confirmLabel: t("Discard"),
      tone: "danger",
    });
  const pathname = usePathname();
  const [open, setOpen] = useState(false);
  // Three separate areas, because the header offers two different doors into
  // this panel: landing on "Model settings" after asking for accounts made
  // them look like the same thing.
  const [tab, setTab] = useState<"task" | "activity" | "connections" | "model">(
    "task",
  );
  const [providers, setProviders] = useState<Provider[]>([]);
  const [supervision, setSupervision] = useState<
    "none" | "shadow" | "supervised"
  >("none");
  const [teacherGrant, setTeacherGrant] = useState("");
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
  // null: no limit on the run's total (each request still reserves its own).
  const [maxTokens, setMaxTokens] = useState<number | null>(16000);
  // The snapshot cap is a guard against copying an unreasonable amount of a
  // dataset, not a knob a person tunes per task.
  const storageMiB = 512;
  const [providerKind, setProviderKind] =
    useState<ProviderKind>("openai-compatible");
  const localKind = providerKind !== "openai-compatible";
  const [modelDigest, setModelDigest] = useState<string | null>(null);
  const [contextTokens, setContextTokens] = useState(8192);
  // Local models: a server's per-request image limit, the longest image side
  // sent, reasoning before the answer, no system role, and the prompt style.
  const [maxImages, setMaxImages] = useState<number | null>(null);
  const [imageMaxSide, setImageMaxSide] = useState<number | null>(null);
  const [think, setThink] = useState(false);
  const [foldSystem, setFoldSystem] = useState(false);
  const [promptStyle, setPromptStyle] = useState<"full" | "lean">("full");
  // Not edited here; kept so saving a profile does not reset it.
  const [inFlight, setInFlight] = useState(1);
  const [name, setName] = useState("model");
  const [url, setUrl] = useState("");
  const [model, setModel] = useState("");
  const [keyEnv, setKeyEnv] = useState("LEVI_MODEL_API_KEY");
  const [vision, setVision] = useState(false);
  const [supportsTools, setSupportsTools] = useState(false);
  const [allowLocal, setAllowLocal] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const toast = useToast();
  const [draftEdited, setDraftEdited] = useState(false);
  const [pilotRuntime, setPilotRuntime] = useState<"" | "codex" | "claude">("");
  const [follow, setFollow] = useState(false);
  const [refreshVersion, setRefreshVersion] = useState(0);
  const [activeEvidence, setActiveEvidence] = useState<Evidence | null>(null);
  const [tasks, setTasks] = useState<string[]>([]);
  const facets = useDatasetFacets(repo);
  const [width, setWidth] = useState<number | null>(null);
  const dragFrom = useRef<{ x: number; width: number } | null>(null);
  // The window width bounds the drawer (aria-valuemax); read after mount.
  const [viewport, setViewport] = useState(PANEL_DEFAULT + PANEL_GUTTER);
  useEffect(() => {
    const update = () => setViewport(window.innerWidth);
    update();
    window.addEventListener("resize", update);
    return () => window.removeEventListener("resize", update);
  }, []);

  useEffect(() => {
    const saved = Number(readBrowserStorage("local", "levi-agent-width"));
    if (saved >= PANEL_MIN) setWidth(saved);
  }, []);
  useEffect(() => {
    if (width) writeBrowserStorage("local", "levi-agent-width", String(width));
  }, [width]);

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
      setTab("connections");
      setOpen(true);
    };
    window.addEventListener(SHELL_EVENTS.agentConnections, connections);
    window.addEventListener(SHELL_EVENTS.agentToggle, toggle);
    return () => {
      window.removeEventListener(SHELL_EVENTS.agentToggle, toggle);
      window.removeEventListener(SHELL_EVENTS.agentConnections, connections);
    };
  }, []);
  // The header shows whether the drawer is open (its button is a toggle).
  useEffect(() => {
    window.dispatchEvent(
      new CustomEvent(SHELL_EVENTS.agentState, { detail: { open } }),
    );
  }, [open]);
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
    supervision:
      pilotRuntime || ["external", "local-tools"].includes(provider)
        ? "none"
        : supervision,
    teacher_grant: teacherGrant || null,
    mode,
    // On the MCP channel the consent is the connection itself: you created
    // it, it names the datasets it may touch, and the frames go to your own
    // agent rather than to a third-party endpoint LEVI uploads to.
    allow_media_egress: provider === "external" ? true : egress,
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
    toast.show({
      title: t(
        "Evidence navigation stays in the current episode. Open other episodes manually after saving your edits.",
      ),
      tone: "info",
    });
  }
  // A drawer beside the page (not modal): evidence seeks and annotation edits
  // on the page stay possible while it is open, as with the earlier dock.
  const supervisionPicker = !pilotRuntime &&
    !["external", "local-tools"].includes(provider) && (
      <TeacherChoice
        dataset={repo}
        mode={supervision}
        teacher={teacherGrant}
        change={(mode, teacher) => {
          setSupervision(mode);
          setTeacherGrant(teacher);
        }}
      />
    );
  const approved = !!run?.plan?.approval;
  const pilotAccepted = !!run?.plan?.pilot_review?.accepted;
  const approveFirst = t("Approve the execution plan first.");
  return (
    <Sheet
      open={open}
      onClose={() => setOpen(false)}
      title={t("Agent Workbench")}
      side="right"
      modal={false}
      width={width}
      className="levi-agent-sheet"
    >
      <T>
        <div className="ag-dock">
          {/* Drag the left edge to widen the panel; a long evidence path or a
            diff is unreadable at a fixed width. The chosen width is kept per
            browser. */}
          <div
            className="levi-agent-grip"
            role="separator"
            aria-orientation="vertical"
            aria-label={t("Resize panel")}
            tabIndex={0}
            onPointerDown={(event) => {
              event.currentTarget.setPointerCapture(event.pointerId);
              dragFrom.current = {
                x: event.clientX,
                width: width || event.currentTarget.parentElement!.clientWidth,
              };
            }}
            onPointerMove={(event) => {
              if (!dragFrom.current) return;
              const next =
                dragFrom.current.width + (dragFrom.current.x - event.clientX);
              setWidth(clampPanelWidth(next, window.innerWidth));
            }}
            onPointerUp={(event) => {
              event.currentTarget.releasePointerCapture(event.pointerId);
              dragFrom.current = null;
            }}
            onKeyDown={(event) => {
              const next = panelWidthForKey(
                event.key,
                event.shiftKey,
                width,
                window.innerWidth,
              );
              if (next === null) return;
              event.preventDefault();
              setWidth(next);
            }}
            {...panelAria(width, viewport)}
            aria-valuetext={`${panelAria(width, viewport)["aria-valuenow"]} px`}
          />
          <p className="ag-muted">
            Sampled evidence · every suggestion is reviewed by you
          </p>
          <Tabs
            className="ag-tabs"
            label={t("Agent Workbench sections")}
            value={tab}
            onChange={(id) => setTab(id as typeof tab)}
            items={[
              { id: "task", label: t("Tasks & review") },
              { id: "activity", label: t("Live activity") },
              { id: "connections", label: t("Accounts & connections") },
              { id: "model", label: t("Model settings") },
            ]}
          />
          {error && (
            <RequestProblem
              action="That action did not complete"
              message={friendlyError(error, t)}
            />
          )}
          {tab === "activity" ? (
            <AgentActivity open={open && tab === "activity"} />
          ) : tab === "connections" ? (
            <>
              <Field label={t("Execution channel")}>
                <Select
                  value={pilotRuntime}
                  onChange={(e) => {
                    setPilotRuntime(e.target.value as "" | "codex" | "claude");
                    if (e.target.value) setProvider("external");
                  }}
                >
                  <option value="">{t("API / external MCP")}</option>
                  <option value="codex">Codex Pilot</option>
                  <option value="claude">Claude Code Pilot</option>
                </Select>
              </Field>
              <AgentRuntimeConnections />
              <AgentConnections
                providers={providers}
                selected={provider}
                select={setProvider}
                refresh={async () =>
                  setProviders(await api<Provider[]>("/providers"))
                }
                edit={(p) => {
                  setTab("model");
                  setProviderKind(p.kind ?? "openai-compatible");
                  setModelDigest(p.model_digest ?? null);
                  setContextTokens(p.context_tokens ?? 8192);
                  setMaxImages(p.max_images ?? null);
                  setImageMaxSide(p.image_max_side ?? null);
                  setThink(p.think ?? false);
                  setFoldSystem(p.fold_system ?? false);
                  setPromptStyle(p.prompt_style ?? "full");
                  setInFlight(p.requests_in_flight ?? 1);
                  setName(p.name);
                  setUrl(p.base_url);
                  setModel(p.model);
                  setKeyEnv(p.key_env);
                  setVision(p.vision);
                  setSupportsTools(p.tools);
                  setAllowLocal(p.allow_localhost);
                }}
              />
            </>
          ) : tab === "model" ? (
            <form
              className="ag-form"
              onSubmit={(e) => {
                e.preventDefault();
                void act(async () => {
                  await api("/providers", {
                    name,
                    kind: providerKind,
                    model_digest: modelDigest,
                    context_tokens: contextTokens,
                    base_url: url,
                    model,
                    key_env: keyEnv,
                    vision,
                    tools: supportsTools,
                    structured_output: localKind,
                    allow_localhost: allowLocal,
                    // Local-model settings only where their inputs show.
                    max_images: localKind ? maxImages : null,
                    image_max_side: localKind ? imageMaxSide : null,
                    think: localKind && think,
                    fold_system: providerKind === "openai-local" && foldSystem,
                    // Saving replaces the whole profile: send the style shown.
                    prompt_style: localKind ? promptStyle : "full",
                    requests_in_flight: localKind ? inFlight : 1,
                  });
                  setProviders(await api<Provider[]>("/providers"));
                  setProvider(name);
                  toast.show({
                    title: t("Provider saved"),
                    description: t("Credentials stay on the server."),
                    tone: "success",
                  });
                });
              }}
            >
              <Field label={t("Connection type")}>
                <Select
                  value={providerKind}
                  onChange={(e) => {
                    const kind = e.target.value as ProviderKind;
                    setProviderKind(kind);
                    setModelDigest(null);
                    if (kind === "openai-compatible") {
                      setKeyEnv((v) =>
                        v === "LEVI_LOCAL_MODEL_KEY" ? "LEVI_MODEL_API_KEY" : v,
                      );
                    }
                    if (kind === "ollama") {
                      setName("ollama-local");
                      setUrl("http://127.0.0.1:11434");
                      setModel("qwen3.5:4b");
                      setAllowLocal(true);
                      setSupportsTools(false);
                    }
                    if (kind === "openai-local") {
                      setName("vllm-local");
                      setUrl("http://127.0.0.1:8100");
                      setModel("");
                      setContextTokens(32768);
                      // Its own variable: a cloud key is never sent here.
                      setKeyEnv("LEVI_LOCAL_MODEL_KEY");
                      setAllowLocal(true);
                      setSupportsTools(false);
                    }
                  }}
                >
                  <option value="openai-compatible">
                    {t("OpenAI-compatible API")}
                  </option>
                  <option value="ollama">{t("Ollama · local service")}</option>
                  <option value="openai-local">
                    {t("Local OpenAI-compatible server (vLLM)")}
                  </option>
                </Select>
              </Field>
              <Field label={t("Provider name")} required>
                <Input value={name} onChange={(e) => setName(e.target.value)} />
              </Field>
              <Field
                label={t(
                  providerKind === "ollama"
                    ? "Ollama service URL"
                    : providerKind === "openai-local"
                      ? "Local model server URL"
                      : "Compatible API base URL",
                )}
                required
              >
                <Input
                  type="url"
                  value={url}
                  onChange={(e) => {
                    setUrl(e.target.value);
                    setModelDigest(null);
                  }}
                  placeholder="https://provider.example/v1"
                />
              </Field>
              <Field label={t("Model ID")} required>
                <Input
                  value={model}
                  onChange={(e) => {
                    setModel(e.target.value);
                    setModelDigest(null);
                  }}
                />
              </Field>
              {providerKind === "openai-compatible" && (
                <>
                  <Field
                    label={t("Server API-key environment variable")}
                    hint={t(
                      "Set this environment variable before starting LEVI. Never paste the key here. Tool-call support is required.",
                    )}
                    required
                  >
                    <Input
                      value={keyEnv}
                      onChange={(e) => setKeyEnv(e.target.value)}
                    />
                  </Field>
                  <Checkbox
                    label={t("Model supports structured tool calls")}
                    checked={supportsTools}
                    onChange={(e) => setSupportsTools(e.target.checked)}
                  />
                </>
              )}
              {providerKind === "ollama" && (
                <Hint>
                  Save this connection, then inspect and bind the installed
                  model in Accounts & connections. No API key is required.
                </Hint>
              )}
              {providerKind === "openai-local" && (
                <>
                  <Hint>
                    Use the server root without /v1. Save this connection, then
                    inspect and bind the served model in Accounts & connections.
                  </Hint>
                  <Field
                    label={t("API-key environment variable (optional)")}
                    required
                  >
                    <Input
                      value={keyEnv}
                      onChange={(e) => setKeyEnv(e.target.value)}
                    />
                  </Field>
                  <Checkbox
                    label={t(
                      "Send the system prompt in the first user turn (templates without a system role)",
                    )}
                    checked={foldSystem}
                    onChange={(e) => setFoldSystem(e.target.checked)}
                  />
                </>
              )}
              {localKind && (
                <>
                  <Field label={t("Context tokens")} required>
                    <Input
                      type="number"
                      min={1024}
                      max={131072}
                      value={contextTokens}
                      onChange={(e) => setContextTokens(Number(e.target.value))}
                    />
                  </Field>
                  <Field label={t("Images per request (empty: no limit)")}>
                    <Input
                      type="number"
                      min={1}
                      max={1000}
                      value={maxImages ?? ""}
                      onChange={(e) =>
                        setMaxImages(
                          e.target.value ? Number(e.target.value) : null,
                        )
                      }
                    />
                  </Field>
                  <Field
                    label={t("Longest image side sent, pixels (empty: native)")}
                  >
                    <Input
                      type="number"
                      min={128}
                      max={4096}
                      value={imageMaxSide ?? ""}
                      onChange={(e) =>
                        setImageMaxSide(
                          e.target.value ? Number(e.target.value) : null,
                        )
                      }
                    />
                  </Field>
                  <Checkbox
                    label={t("Let the model reason before answering")}
                    checked={think}
                    onChange={(e) => setThink(e.target.checked)}
                  />
                  <Field label={t("Prompt style")}>
                    <Select
                      value={promptStyle}
                      onChange={(e) =>
                        setPromptStyle(e.target.value as "full" | "lean")
                      }
                    >
                      <option value="full">
                        {t("Full: LEVI skills and context (dataset review)")}
                      </option>
                      <option value="lean">
                        {t(
                          "Lean: instruction and frame list (temporal annotation)",
                        )}
                      </option>
                    </Select>
                  </Field>
                </>
              )}
              <Checkbox
                label={t("Model supports image input")}
                checked={vision}
                onChange={(e) => setVision(e.target.checked)}
              />
              <Checkbox
                label={t(
                  "Allow an explicitly configured loopback model endpoint",
                )}
                checked={allowLocal}
                onChange={(e) => setAllowLocal(e.target.checked)}
              />
              <Actions>
                <Button
                  type="submit"
                  variant="primary"
                  size="sm"
                  icon={Save}
                  loading={busy}
                >
                  {t("Save model settings")}
                </Button>
              </Actions>
              {providers.length > 0 && (
                <ul className="ag-list ag-list--plain">
                  {providers.map((p) => (
                    <li key={p.name}>
                      {p.name} ·{" "}
                      <Badge tone={p.credential_ready ? "success" : "warning"}>
                        {t(
                          p.credential_ready
                            ? "Credential available"
                            : "Credential missing",
                        )}
                      </Badge>
                    </li>
                  ))}
                </ul>
              )}
            </form>
          ) : (
            <>
              <AgentTaskConsole
                providers={providers}
                supervision={supervision}
                teacherGrant={teacherGrant}
              />
              <Disclosure
                open={!run}
                icon={FilePlus2}
                summary={t("New task · frozen scope")}
              >
                <form
                  className="ag-form"
                  onSubmit={async (e) => {
                    e.preventDefault();
                    if (draftEdited && !(await discardDraft())) return;
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
                  {supervisionPicker}
                  <Field label={t("Agent model")} required>
                    <Select
                      value={provider}
                      onChange={(e) => setProvider(e.target.value)}
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
                    </Select>
                  </Field>
                  <Field label={t("Dataset ID")} required>
                    <Input
                      value={repo}
                      onChange={(e) => setRepo(e.target.value)}
                      placeholder="local/dataset or org/dataset"
                    />
                  </Field>
                  {facets.tasks.length > 1 && (
                    <div className="ag-field">
                      <span className="ag-label">{t("Tasks")}</span>
                      <ChipMultiSelect
                        label={t("Tasks")}
                        options={facets.tasks.map((task) => ({
                          value: task,
                          label:
                            task.length > 46 ? `${task.slice(0, 46)}…` : task,
                          hint: task,
                        }))}
                        selected={tasks}
                        onChange={setTasks}
                        columns
                      />
                      <Hint>
                        {t(
                          "This dataset declares several tasks. Choosing some is a note for the reader; the scope that is frozen is the episodes below.",
                        )}
                      </Hint>
                    </div>
                  )}
                  <div className="ag-field">
                    <span className="ag-label">{t("Episodes")}</span>
                    <ChipMultiSelect
                      label={t("Episodes")}
                      options={facets.episodes.map((index) => ({
                        value: String(index),
                        label: String(index),
                      }))}
                      selected={episodes
                        .split(",")
                        .map((part) => part.trim())
                        .filter(Boolean)}
                      onChange={(next) => setEpisodes(next.join(","))}
                      emptyHint={t(
                        "Enter a dataset ID above to list its episodes.",
                      )}
                    />
                    <Hint>
                      {t(
                        "Click an episode, or drag across several. The chosen set is frozen when the plan is created and cannot grow later.",
                      )}
                    </Hint>
                  </div>
                  <div className="ag-field">
                    <span className="ag-label">{t("Cameras")}</span>
                    <ChipMultiSelect
                      label={t("Cameras")}
                      options={facets.cameras.map((key) => ({
                        value: key,
                        label: key.replace(/^observation\.images\./, ""),
                        hint: key,
                      }))}
                      selected={cameras
                        .split(",")
                        .map((part) => part.trim())
                        .filter(Boolean)}
                      onChange={(next) => setCameras(next.join(","))}
                      emptyHint={t("This dataset declares no video cameras.")}
                    />
                    <Hint>
                      {t(
                        "Only frames from the chosen cameras are read. Subtask and object work needs at least one.",
                      )}
                    </Hint>
                  </div>
                  <Field
                    label={t("Task instructions")}
                    hint={t(
                      "Say what to look for and how to judge it. This text is given to the agent with the evidence; it is not a search query.",
                    )}
                    required
                  >
                    <Textarea
                      value={instruction}
                      onChange={(e) => setInstruction(e.target.value)}
                      rows={4}
                      placeholder={t("INSTRUCTION_TEMPLATE")}
                    />
                  </Field>
                  <Field label={t("Task type")}>
                    <Select
                      value={workflow}
                      onChange={(e) => setWorkflow(e.target.value)}
                    >
                      <option value="review">{t("Dataset review")}</option>
                      <option value="temporal">
                        {t("Video subtasks and events")}
                      </option>
                      <option value="objects">
                        {t("Visible object masks")}
                      </option>
                    </Select>
                  </Field>
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
                      <Actions>
                        <Button
                          size="sm"
                          icon={Plus}
                          onClick={() =>
                            setDefinitions(
                              JSON.stringify(
                                [
                                  {
                                    id: "pick",
                                    label: "Pick",
                                    definition: "Lift the target object",
                                    starts_when: "Gripper approaches target",
                                    ends_when:
                                      "Attempt completes or is abandoned",
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
                          {t("Insert editable definition example")}
                        </Button>
                      </Actions>
                      <Field label={t("Coarse observation step (seconds)")}>
                        <Input
                          type="number"
                          min={0.05}
                          max={60}
                          step={0.05}
                          value={step}
                          onChange={(e) => setStep(Number(e.target.value))}
                        />
                      </Field>
                      <Field label={t("Maximum evidence frames")}>
                        <Input
                          type="number"
                          min={3}
                          max={1000}
                          value={frameCap}
                          onChange={(e) => setFrameCap(Number(e.target.value))}
                        />
                      </Field>
                    </>
                  )}
                  {workflow === "objects" && (
                    <Field label={t("Target object concepts")}>
                      <Input
                        value={concepts}
                        onChange={(e) => setConcepts(e.target.value)}
                        placeholder="cup, plate"
                      />
                    </Field>
                  )}
                  {clarifications.map((q) => (
                    <RequestProblem
                      key={q}
                      action="The plan needs one more answer"
                      message={q}
                    />
                  ))}
                  <div className="ag-field">
                    <span className="ag-label">{t("Working mode")}</span>
                    <SegmentedControl
                      label={t("Working mode")}
                      value={mode}
                      onChange={setMode}
                      options={[
                        { value: "draft", label: t("Produce annotations") },
                        { value: "read_only", label: t("Read only") },
                      ]}
                    />
                    <Hint>
                      {t(
                        "Produce annotations: the agent proposes and you review before anything is published. Read only: it may look but not propose.",
                      )}
                    </Hint>
                  </div>
                  {provider === "external" ? (
                    <Hint>
                      {t(
                        "Your agent reads the evidence through the connection you created, which already names the datasets it may touch, and spends its own tokens — so LEVI sets no call or token limit here. plans.estimate prices a scope before you start.",
                      )}
                    </Hint>
                  ) : (
                    <Disclosure summary={t("Spending limits for this model")}>
                      <div className="ag-form">
                        <Hint>
                          {t(
                            "These cap what LEVI itself spends at the model endpoint, and stop the run when reached.",
                          )}
                        </Hint>
                        <div className="ag-grid2">
                          <Field label={t("Model calls")}>
                            <Input
                              type="number"
                              min={1}
                              max={1000}
                              value={maxCalls}
                              onChange={(e) =>
                                setMaxCalls(Number(e.target.value))
                              }
                            />
                          </Field>
                          <Field label={t("Tokens")}>
                            <Input
                              type="number"
                              min={256}
                              placeholder={t("No limit")}
                              value={maxTokens ?? ""}
                              onChange={(e) =>
                                setMaxTokens(
                                  e.target.value === ""
                                    ? null
                                    : Number(e.target.value),
                                )
                              }
                            />
                          </Field>
                        </div>
                        <Checkbox
                          label={t(
                            "Send the chosen frames to this model endpoint",
                          )}
                          description={t(
                            "Required before LEVI uploads any frame to a model you configured. Leave it off and the run stays text-only.",
                          )}
                          checked={egress}
                          onChange={(e) => setEgress(e.target.checked)}
                        />
                      </div>
                    </Disclosure>
                  )}
                  <Hint>
                    {t(
                      "Saved annotations only. Unsaved changes in the editor are never submitted or overwritten.",
                    )}
                  </Hint>
                  <Actions>
                    <GatedButton
                      type="submit"
                      size="sm"
                      icon={ListChecks}
                      loading={busy}
                      reason={
                        !provider ? t("Choose an agent model first.") : null
                      }
                    >
                      {t("Inspect & create plan")}
                    </GatedButton>
                  </Actions>
                </form>
              </Disclosure>
              <Field label={t("Open an existing task")}>
                <Select
                  value={selected || ""}
                  onChange={async (e) => {
                    const next = e.target.value;
                    if (draftEdited && !(await discardDraft())) return;
                    setSelected(next);
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
                </Select>
              </Field>
              {run && (
                <section className="ag-section">
                  <h3>{run.context.repo_id}</h3>
                  <p>
                    <Badge tone="neutral">{t(run.status)}</Badge>
                  </p>
                  <Progress
                    label={`${run.completed.length}/${run.context.episodes.length} ${t("episodes processed")}`}
                    value={run.completed.length}
                    max={run.context.episodes.length}
                  />
                  <dl className="ag-stats">
                    <div>
                      <dt>{t("Snapshot MiB")}</dt>
                      <dd>{(run.snapshot_bytes / 1048576).toFixed(1)}</dd>
                    </div>
                    <div>
                      <dt>{t("Calls")}</dt>
                      <dd>{run.requests}</dd>
                    </div>
                    <div>
                      <dt>{t("Tokens")}</dt>
                      <dd>{run.tokens}</dd>
                    </div>
                    <div>
                      <dt>{t("Reserved tokens")}</dt>
                      <dd>{run.reserved_tokens}</dd>
                    </div>
                    <div>
                      <dt>{t("Cache hits")}</dt>
                      <dd>{run.cache_hits || 0}</dd>
                    </div>
                  </dl>
                  {run.reason && <p role="status">{t(run.reason)}</p>}
                  {run.context.supervision &&
                    run.context.supervision !== "none" && (
                      <TeachingStatus runId={run.id} status={run.status} />
                    )}
                  <Disclosure
                    icon={ShieldCheck}
                    summary={t("Approved scope and policy")}
                  >
                    <pre className="ag-pre">
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
                  </Disclosure>
                  {![
                    "running",
                    "queued",
                    "succeeded",
                    "partially_succeeded",
                    "cancelled",
                  ].includes(run.status) &&
                    run.plan && (
                      <Disclosure
                        summary={t("Revise budget and require new approval")}
                      >
                        <div className="ag-form">
                          <Hint>
                            {t("Budget-only revision retains completed work.")}
                          </Hint>
                          <Field label={t("Call limit")}>
                            <Input
                              type="number"
                              min={1}
                              value={maxCalls}
                              onChange={(e) =>
                                setMaxCalls(Number(e.target.value))
                              }
                            />
                          </Field>
                          <Field label={t("Token limit")}>
                            <Input
                              type="number"
                              min={256}
                              placeholder={t("No limit")}
                              value={maxTokens ?? ""}
                              onChange={(e) =>
                                setMaxTokens(
                                  e.target.value === ""
                                    ? null
                                    : Number(e.target.value),
                                )
                              }
                            />
                          </Field>
                          <Actions>
                            <Button
                              size="sm"
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
                              {t("Revise budget and require new approval")}
                            </Button>
                          </Actions>
                        </div>
                      </Disclosure>
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
                  <Actions>
                    {run.context.workflow?.kind === "objects" && (
                      <GatedButton
                        size="sm"
                        icon={ScanSearch}
                        disabled={busy}
                        reason={!approved ? approveFirst : null}
                        onClick={() =>
                          void act(async () => {
                            await tool("runs.prepare", { run_id: run.id });
                          })
                        }
                      >
                        {t("Prepare object evidence without model calls")}
                      </GatedButton>
                    )}
                    {["blocked", "paused", "interrupted", "cancelled"].includes(
                      run.status,
                    ) &&
                      run.completed.length > 0 && (
                        <Button
                          size="sm"
                          disabled={busy}
                          onClick={() =>
                            void act(async () => {
                              await tool("runs.finish", { run_id: run.id });
                            })
                          }
                        >
                          {t("Read completed work")}
                        </Button>
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
                          <GatedButton
                            size="sm"
                            icon={Play}
                            variant={pilotAccepted ? "secondary" : "primary"}
                            disabled={busy}
                            reason={!approved ? approveFirst : null}
                            onClick={() =>
                              void act(async () => {
                                await tool("runs.execute", {
                                  run_id: run.id,
                                  pilot: true,
                                });
                              })
                            }
                          >
                            {t("Run pilot")}
                          </GatedButton>
                          <GatedButton
                            size="sm"
                            icon={Play}
                            variant={pilotAccepted ? "primary" : "secondary"}
                            disabled={busy}
                            reason={
                              !pilotAccepted
                                ? t("Accept the pilot quality first.")
                                : null
                            }
                            onClick={() =>
                              void act(async () => {
                                await tool("runs.resume", {
                                  run_id: run.id,
                                  pilot: false,
                                });
                              })
                            }
                          >
                            {t("Execute remaining")}
                          </GatedButton>
                        </>
                      )}
                    {["running", "queued"].includes(run.status) && (
                      <Button
                        size="sm"
                        icon={Pause}
                        disabled={busy}
                        onClick={() =>
                          void act(async () => {
                            await tool("runs.pause", { run_id: run.id });
                          })
                        }
                      >
                        {t("Pause at boundary")}
                      </Button>
                    )}
                    {!["succeeded", "cancelled"].includes(run.status) && (
                      <Button
                        size="sm"
                        variant="ghost"
                        className="ag-danger"
                        icon={Ban}
                        disabled={busy}
                        onClick={() =>
                          void act(async () => {
                            await tool("runs.cancel", { run_id: run.id });
                          })
                        }
                      >
                        {t("Cancel task")}
                      </Button>
                    )}
                  </Actions>
                  <Checkbox
                    label={t("Follow evidence in the current episode")}
                    checked={follow}
                    onChange={(e) => setFollow(e.target.checked)}
                  />
                  {evidence.length > 0 && (
                    <Disclosure
                      icon={ImageIcon}
                      summary={t("Sampled evidence")}
                    >
                      <div className="ag-evidence">
                        {evidence.map((row) => (
                          <Button
                            key={row.id}
                            size="sm"
                            className="ag-evidence__item"
                            onClick={() => seek(row)}
                          >
                            {/* Native image artifacts are authenticated same-origin resources. */}
                            {row.artifact && (
                              // eslint-disable-next-line @next/next/no-img-element
                              <img
                                src={`${base}/runs/${run.id}/artifacts/${row.artifact}`}
                                alt={`${row.camera_key} / ${row.frame_index}`}
                              />
                            )}
                            <span>
                              {row.episode_index} · {row.timestamp.toFixed(3)}s
                              · {row.camera_key}
                            </span>
                          </Button>
                        ))}
                      </div>
                    </Disclosure>
                  )}
                  <AgentObjectTool
                    key={run.id}
                    runId={run.id}
                    status={run.status}
                    jobIds={change?.object_jobs || []}
                    refresh={() => setRefreshVersion((v) => v + 1)}
                  />
                  {activeEvidence?.artifact && (
                    <div className="ag-review-evidence">
                      {/* eslint-disable-next-line @next/next/no-img-element */}
                      <img
                        src={`${base}/runs/${run.id}/artifacts/${activeEvidence.artifact}`}
                        alt={`${activeEvidence.camera_key} / ${activeEvidence.frame_index}`}
                      />
                      <p className="ag-muted">
                        {activeEvidence.camera_key} ·{" "}
                        {activeEvidence.timestamp.toFixed(3)}s
                      </p>
                    </div>
                  )}
                  {change && (
                    <section
                      className="ag-section"
                      aria-label={t("Review proposed changes")}
                    >
                      <h3>{t("Review proposed changes")}</h3>
                      <AgentQuality runId={run.id} revision={change.revision} />
                      <p>
                        <Badge tone="neutral">{t(change.status)}</Badge>{" "}
                        <span className="ag-muted">
                          <T>Base revision</T>: {change.base_revision}
                        </span>
                      </p>
                      <Hint>
                        Sparse samples are not full-episode coverage. Verify
                        boundaries and outcomes before approval.
                      </Hint>
                      {change.undo_of && (
                        <p>
                          Inverse ChangeSet · restores the previous saved
                          annotations.
                        </p>
                      )}
                      {change.status !== "committed" && (
                        <Hint>
                          Approval accepts remaining language suggestions;
                          rejected items stay excluded. Object masks require
                          separate decisions.
                        </Hint>
                      )}
                      <AgentReviewQueue
                        proposals={change.proposals}
                        decisions={change.decisions || {}}
                        disabled={busy || change.status === "committed"}
                        closedReason={
                          change.status === "committed"
                            ? t(
                                "These changes are committed, so decisions are closed.",
                              )
                            : undefined
                        }
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
                            toast.show({
                              title: t(
                                "Save draft edits before recording review decisions.",
                              ),
                              tone: "warning",
                            });
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
                      <Actions>
                        {change.status === "committed" && (
                          <Button
                            size="sm"
                            icon={Download}
                            disabled={busy}
                            onClick={() =>
                              void act(async () => {
                                const result = await tool<{
                                  output_dir: string;
                                }>("export.run", { run_id: run.id });
                                toast.show({
                                  title: t("Exported"),
                                  description: result.output_dir,
                                  tone: "success",
                                });
                              })
                            }
                          >
                            {t("Export full dataset with reviewed changes")}
                          </Button>
                        )}
                        {change.status === "committed" && !change.undo_of && (
                          <Button
                            size="sm"
                            icon={Undo2}
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
                            {t("Create undo draft")}
                          </Button>
                        )}
                        {draftEdited && (
                          <Button
                            size="sm"
                            icon={Save}
                            disabled={busy}
                            onClick={() =>
                              void act(async () => {
                                const next = await tool<Change>(
                                  "changes.edit",
                                  {
                                    changeset_id: change.id,
                                    revision: change.revision,
                                    proposals: change.proposals,
                                  },
                                );
                                setChange(next);
                                setDraftEdited(false);
                              })
                            }
                          >
                            {t("Save draft edits")}
                          </Button>
                        )}
                        {!draftEdited && change.status !== "committed" && (
                          <>
                            <HumanActionMark />
                            <Button
                              size="sm"
                              icon={Check}
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
                              {t("Validate & approve")}
                            </Button>
                          </>
                        )}
                        {!draftEdited && change.status === "approved" && (
                          <>
                            <HumanActionMark />
                            <Button
                              size="sm"
                              variant="primary"
                              icon={CircleStop}
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
                                  toast.show({
                                    title: t(
                                      "Committed. Reload saved annotations to see the new revision.",
                                    ),
                                    tone: "success",
                                  });
                                })
                              }
                            >
                              {t("Commit approved changes")}
                            </Button>
                          </>
                        )}
                      </Actions>
                    </section>
                  )}
                </section>
              )}
            </>
          )}
        </div>
      </T>
    </Sheet>
  );
}
