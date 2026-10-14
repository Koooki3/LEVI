"use client";
/**
 * The Agent Workbench's model settings: the provider profile being edited
 * (its fields, and filling them from a saved profile) and the form that saves
 * it. Moved verbatim out of agent-workbench.tsx.
 */
import { useState } from "react";
import { Save } from "lucide-react";
import { useLocale } from "./levi-locale";
import { Badge, Button, Checkbox, Field, Input, Select, useToast } from "./ds";
import { Actions, Hint } from "./agent-ui";
import { api, type Provider, type ProviderKind } from "./agent-workbench-api";

export function useProviderForm() {
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

  /** Fill the form from a saved profile (the connections list's Edit). */
  function fill(p: Provider) {
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
  }

  return {
    providerKind,
    modelDigest,
    contextTokens,
    maxImages,
    imageMaxSide,
    think,
    foldSystem,
    promptStyle,
    inFlight,
    name,
    url,
    model,
    keyEnv,
    vision,
    supportsTools,
    allowLocal,
    setProviderKind,
    setModelDigest,
    setContextTokens,
    setMaxImages,
    setImageMaxSide,
    setThink,
    setFoldSystem,
    setPromptStyle,
    setInFlight,
    setName,
    setUrl,
    setModel,
    setKeyEnv,
    setVision,
    setSupportsTools,
    setAllowLocal,
    localKind,
    fill,
  };
}

export type ProviderFormState = ReturnType<typeof useProviderForm>;

export function ProviderForm({
  form: providerForm,
  act,
  busy,
  providers,
  onSaved,
}: {
  form: ProviderFormState;
  act: (fn: () => Promise<void>) => Promise<void>;
  busy: boolean;
  providers: Provider[];
  onSaved: (providers: Provider[], saved: string) => void;
}) {
  const { t } = useLocale();
  const toast = useToast();
  const {
    providerKind,
    modelDigest,
    contextTokens,
    maxImages,
    imageMaxSide,
    think,
    foldSystem,
    promptStyle,
    inFlight,
    name,
    url,
    model,
    keyEnv,
    vision,
    supportsTools,
    allowLocal,
    setProviderKind,
    setModelDigest,
    setContextTokens,
    setMaxImages,
    setImageMaxSide,
    setThink,
    setFoldSystem,
    setPromptStyle,
    setName,
    setUrl,
    setModel,
    setKeyEnv,
    setVision,
    setSupportsTools,
    setAllowLocal,
    localKind,
  } = providerForm;
  return (
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
          onSaved(await api<Provider[]>("/providers"), name);
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
            <Input value={keyEnv} onChange={(e) => setKeyEnv(e.target.value)} />
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
          Save this connection, then inspect and bind the installed model in
          Accounts & connections. No API key is required.
        </Hint>
      )}
      {providerKind === "openai-local" && (
        <>
          <Hint>
            Use the server root without /v1. Save this connection, then inspect
            and bind the served model in Accounts & connections.
          </Hint>
          <Field label={t("API-key environment variable (optional)")} required>
            <Input value={keyEnv} onChange={(e) => setKeyEnv(e.target.value)} />
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
                setMaxImages(e.target.value ? Number(e.target.value) : null)
              }
            />
          </Field>
          <Field label={t("Longest image side sent, pixels (empty: native)")}>
            <Input
              type="number"
              min={128}
              max={4096}
              value={imageMaxSide ?? ""}
              onChange={(e) =>
                setImageMaxSide(e.target.value ? Number(e.target.value) : null)
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
                {t("Lean: instruction and frame list (temporal annotation)")}
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
        label={t("Allow an explicitly configured loopback model endpoint")}
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
  );
}
