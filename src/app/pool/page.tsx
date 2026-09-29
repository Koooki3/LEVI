"use client";
import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useLocale } from "@/components/levi-locale";
import { leviRequest } from "@/components/levi-api";
import {
  EMPTY_FILTERS,
  FacetsPanel,
  type Filters,
} from "@/components/pool/facets-panel";
import { EpisodeTable, TaskTable } from "@/components/pool/pool-tables";
import {
  CompositionPanel,
  PoolWarnings,
} from "@/components/pool/composition-panel";
import { ExportPanel } from "@/components/pool/export-panel";
import { PushDialog } from "@/components/pool/push-dialog";
import {
  PoolJobProgress,
  RUNNING,
  StatusBadge,
} from "@/components/pool/pool-progress";
import { defaultTiming } from "@/components/pool/types";
import type {
  EpisodeRow,
  ExportFormat,
  Facets,
  PoolJob,
  PickedEpisodes,
  PoolStatus,
  Preview,
  Recipe,
  Suggest,
  TaskEntry,
  TaskRow,
  Timing,
} from "@/components/pool/types";

const PAGE = 50;

const EMPTY_RECIPE: Recipe = {
  name: "",
  categories: [],
  sources: [],
  tasks: [],
  outcome: "all",
  per_task_cap: null,
  seed: 0,
  date_from: null,
  date_to: null,
  policies: [],
  policy_models: [],
  policy_checkpoints: [],
  policy_methods: [],
  include_nonstandard: false,
  exclude: [],
};

function query(filters: Filters, extra: Record<string, string> = {}) {
  const q = new URLSearchParams();
  for (const c of filters.categories) q.append("category", c);
  for (const s of filters.sources) q.append("source", s);
  for (const p of filters.policies) q.append("policy", p);
  for (const p of filters.policyModels) q.append("policy_model", p);
  for (const p of filters.policyCheckpoints) q.append("policy_checkpoint", p);
  for (const p of filters.policyMethods) q.append("policy_method", p);
  if (filters.search) q.set("search", filters.search);
  if (filters.outcome !== "all") q.set("outcome", filters.outcome);
  if (filters.dateFrom) q.set("date_from", filters.dateFrom);
  if (filters.dateTo) q.set("date_to", filters.dateTo);
  if (filters.showHeldout) q.set("show_heldout", "true");
  if (filters.showCopies) q.set("show_copies", "true");
  if (filters.showArchive) q.set("show_archive", "true");
  for (const [k, v] of Object.entries(extra)) q.append(k, v);
  return q.toString();
}

function when(value: number | string | undefined): string {
  if (!value) return "—";
  const date =
    typeof value === "number" ? new Date(value * 1000) : new Date(value);
  return isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

export default function TrainingPool() {
  const { t } = useLocale();
  const [status, setStatus] = useState<PoolStatus | null>(null);
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS);
  const [facets, setFacets] = useState<Facets | null>(null);
  const [tasks, setTasks] = useState<TaskRow[]>([]);
  const [episodes, setEpisodes] = useState<{
    total: number;
    episodes: EpisodeRow[];
  }>({ total: 0, episodes: [] });
  const [offset, setOffset] = useState(0);
  const [focus, setFocus] = useState<string | null>(null);
  const [composition, setComposition] = useState<Recipe>(EMPTY_RECIPE);
  const [recipes, setRecipes] = useState<Recipe[]>([]);
  const [format, setFormat] = useState<ExportFormat>("lerobot_v21");
  const [humanAsSuccess, setHumanAsSuccess] = useState(false);
  const [fps, setFps] = useState(10);
  // null: the format's own timing (LeRobot resample, RECAP retime).
  const [timing, setTiming] = useState<Timing | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [previewError, setPreviewError] = useState("");
  const [previewing, setPreviewing] = useState(false);
  const [exportJob, setExportJob] = useState<PoolJob | null>(null);
  const [pushFor, setPushFor] = useState<PoolJob | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const recipe: Recipe = useMemo(
    () => ({
      ...composition,
      categories: filters.categories,
      sources: filters.sources,
      policies: filters.policies,
      policy_models: filters.policyModels,
      policy_checkpoints: filters.policyCheckpoints,
      policy_methods: filters.policyMethods,
      outcome: filters.outcome,
      date_from: filters.dateFrom || null,
      date_to: filters.dateTo || null,
    }),
    [composition, filters],
  );

  const chosenNames = useMemo(
    () => composition.tasks.map((e) => e.task),
    [composition.tasks],
  );
  const suggest = useCallback(
    (task: string) =>
      leviRequest<Suggest>("POST", "pool/suggest", {
        recipe: { ...recipe, name: recipe.name || "untitled" },
        task,
        format,
        human_as_success: humanAsSuccess,
      }),
    [recipe, format, humanAsSuccess],
  );
  const listEpisodes = useCallback(
    (task: string) =>
      leviRequest<PickedEpisodes>("POST", "pool/selection", {
        recipe: { ...recipe, name: recipe.name || "untitled" },
        task,
        format,
        human_as_success: humanAsSuccess,
      }),
    [recipe, format, humanAsSuccess],
  );
  const addTask = useCallback(
    (entry: TaskEntry) =>
      setComposition((c) =>
        c.tasks.some((e) => e.task === entry.task)
          ? c
          : { ...c, tasks: [...c.tasks, entry] },
      ),
    [],
  );

  const refreshStatus = useCallback(async () => {
    setStatus(await leviRequest<PoolStatus>("GET", "pool/status"));
  }, []);
  const refreshRecipes = useCallback(async () => {
    const value = await leviRequest<{ recipes: Recipe[] }>(
      "GET",
      "pool/recipes",
    );
    setRecipes(value.recipes);
  }, []);
  useEffect(() => {
    refreshStatus().catch((e) => setError(String(e)));
    refreshRecipes().catch(() => {});
  }, [refreshStatus, refreshRecipes]);
  const anyRunning = !!status?.jobs.some((j) => RUNNING.has(j.status));
  useEffect(() => {
    const timer = setInterval(
      () => refreshStatus().catch(() => {}),
      anyRunning ? 1000 : 5000,
    );
    return () => clearInterval(timer);
  }, [refreshStatus, anyRunning]);

  const scannedAt = status?.last_scan?.scanned_at;
  const scanned = !!scannedAt;
  const filterKey = query(filters);
  // Facets, tasks: whenever the filters or the index change.
  useEffect(() => {
    if (!scanned) return;
    let live = true;
    const toggles = query({
      ...EMPTY_FILTERS,
      categories: filters.categories,
      showHeldout: filters.showHeldout,
      showCopies: filters.showCopies,
      showArchive: filters.showArchive,
    });
    Promise.all([
      leviRequest<Facets>("GET", `pool/facets?${toggles}`),
      leviRequest<{ tasks: TaskRow[] }>("GET", `pool/tasks?${filterKey}`),
    ])
      .then(([f, tk]) => {
        if (!live) return;
        setFacets(f);
        setTasks(tk.tasks);
      })
      .catch((e) => live && setError(String(e)));
    return () => {
      live = false;
    };
  }, [
    filterKey,
    scannedAt,
    scanned,
    filters.categories,
    filters.showHeldout,
    filters.showCopies,
    filters.showArchive,
  ]);
  useEffect(() => setOffset(0), [filterKey, focus]);
  useEffect(() => {
    if (!scanned) return;
    let live = true;
    const extra: Record<string, string> = {
      limit: String(PAGE),
      offset: String(offset),
    };
    if (focus) extra.task = focus;
    leviRequest<{ total: number; episodes: EpisodeRow[] }>(
      "GET",
      `pool/episodes?${query(filters, extra)}`,
    )
      .then((value) => live && setEpisodes(value))
      .catch((e) => live && setError(String(e)));
    return () => {
      live = false;
    };
    // filterKey stands for filters.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filterKey, focus, offset, scannedAt, scanned]);

  // Live preview, debounced.
  const effectiveTiming = timing ?? defaultTiming(format);
  const previewKey = JSON.stringify([
    recipe,
    format,
    humanAsSuccess,
    fps,
    effectiveTiming,
  ]);
  useEffect(() => {
    if (!scanned || recipe.tasks.length === 0) {
      setPreview(null);
      setPreviewError("");
      return;
    }
    setPreviewing(true);
    const timer = setTimeout(() => {
      leviRequest<Preview>("POST", "pool/preview", {
        recipe: { ...recipe, name: recipe.name || "untitled" },
        format,
        human_as_success: humanAsSuccess,
        // How raw captures meet the export rate (raw copies have none).
        ...(effectiveTiming ? { fps, timing: effectiveTiming } : {}),
      })
        .then((value) => {
          setPreview(value);
          setPreviewError("");
        })
        .catch((e) =>
          setPreviewError(e instanceof Error ? e.message : String(e)),
        )
        .finally(() => setPreviewing(false));
    }, 350);
    return () => clearTimeout(timer);
    // previewKey stands for recipe and format.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [previewKey, scanned, scannedAt]);

  // Follow the export job.
  useEffect(() => {
    if (!exportJob || !RUNNING.has(exportJob.status)) return;
    const timer = setInterval(() => {
      leviRequest<PoolJob>(
        "GET",
        `pool/jobs/${encodeURIComponent(exportJob.id)}`,
      )
        .then(setExportJob)
        .catch(() => {});
    }, 1000);
    return () => clearInterval(timer);
  }, [exportJob]);

  async function act(fn: () => Promise<void>) {
    setError("");
    try {
      await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }
  const scanJob = status?.jobs.find((j) => j.kind === "scan");
  const scanRunning = !!scanJob && RUNNING.has(scanJob.status);
  const summary = status?.last_scan;

  return (
    <main className="levi-workbench levi-pool">
      <span className="levi-eyebrow">{t("LEVI / TRAINING POOL")}</span>
      <h1>{t("Training pool")}</h1>
      <p>
        {t(
          "Every dataset under the pool folders, one row per episode. Pick tasks in order, preview what goes in, export a new training set and send it to a training machine. Sources stay read-only; held-out test episodes are never exported.",
        )}
      </p>
      {error && (
        <p className="levi-error" role="alert">
          {t(error)}
        </p>
      )}
      <PoolWarnings warnings={status?.warnings || []} />
      {notice && (
        <p className="levi-pool-notice" role="status">
          {notice}
        </p>
      )}

      <section className="levi-box levi-pool-scan" aria-labelledby="pool-scan">
        <div className="levi-pool-scan-head">
          <div>
            <h2 id="pool-scan">{t("Pool folders")}</h2>
            {status && !status.enabled ? (
              <p>
                {t(
                  "The training pool is idle: set LEVI_POOL_ROOTS to the folders it may read, then restart LEVI.",
                )}
              </p>
            ) : (
              <ul className="levi-pool-roots">
                {status?.roots.map((r) => (
                  <li key={r}>
                    <code>{r}</code>
                  </li>
                ))}
              </ul>
            )}
            <p className="levi-pool-hint">
              {t("Last scan")}:{" "}
              {summary ? when(summary.scanned_at) : t("never")}
              {summary &&
                ` · ${summary.episodes.toLocaleString()} ${t("episodes")} · ${summary.sources.toLocaleString()} ${t("sources")} · ${summary.tasks.toLocaleString()} ${t("Tasks").toLowerCase()}`}
              {status?.heldout_lists.length
                ? ` · ${t("Held-out lists")}: ${status.heldout_lists.length}`
                : ""}
            </p>
          </div>
          <div className="levi-row">
            <button
              type="button"
              className="levi-primary"
              disabled={!status?.enabled || scanRunning}
              onClick={() =>
                void act(async () => {
                  await leviRequest("POST", "pool/scan");
                  await refreshStatus();
                })
              }
            >
              {scanRunning ? t("Scanning…") : t("Scan now")}
            </button>
            {scanRunning && scanJob && (
              <button
                type="button"
                className="levi-secondary"
                onClick={() =>
                  void act(async () => {
                    await leviRequest(
                      "POST",
                      `pool/jobs/${encodeURIComponent(scanJob.id)}/cancel`,
                    );
                    await refreshStatus();
                  })
                }
              >
                {t("Cancel")}
              </button>
            )}
          </div>
        </div>
        {scanJob && RUNNING.has(scanJob.status) && (
          <PoolJobProgress job={scanJob} />
        )}
        {status && status.jobs.length > 0 && (
          <details className="levi-pool-jobs">
            <summary>
              {t("Recent jobs")} ({status.jobs.length})
            </summary>
            <table className="levi-table">
              <tbody>
                {status.jobs.map((j) => (
                  <tr key={j.id}>
                    <td>
                      <code>{j.id}</code>
                    </td>
                    <td>{t(j.kind)}</td>
                    <td>
                      <StatusBadge status={j.status} />
                    </td>
                    <td className="levi-pool-ellipsis">
                      <code>
                        {j.kind === "push"
                          ? j.destination
                          : j.kind === "export"
                            ? j.result?.dataset_path || j.target
                            : ""}
                      </code>
                    </td>
                    <td>
                      {when(j.finished_at || j.started_at || j.planned_at)}
                    </td>
                    <td>
                      {RUNNING.has(j.status) && (
                        <button
                          type="button"
                          className="levi-pool-link"
                          onClick={() =>
                            void act(async () => {
                              await leviRequest(
                                "POST",
                                `pool/jobs/${encodeURIComponent(j.id)}/cancel`,
                              );
                              await refreshStatus();
                            })
                          }
                        >
                          {t("Cancel")}
                        </button>
                      )}
                      {j.kind === "export" && j.status === "succeeded" && (
                        <button
                          type="button"
                          className="levi-pool-link"
                          onClick={() => setPushFor(j)}
                        >
                          {t("Send to remote")}
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </details>
        )}
      </section>

      {!scanned && status?.enabled && (
        <p className="levi-pool-muted">
          {t("The pool has not been scanned yet: press Scan now.")}
        </p>
      )}

      {scanned && (
        <div className="levi-pool-layout">
          <FacetsPanel
            facets={facets}
            filters={filters}
            onChange={setFilters}
          />
          <div className="levi-pool-centre">
            <section className="levi-pool-card" aria-labelledby="pool-tasks">
              <h2 id="pool-tasks">
                {t("Tasks")}{" "}
                <span className="levi-pool-muted">({tasks.length})</span>
              </h2>
              <TaskTable
                tasks={tasks}
                chosen={chosenNames}
                focus={focus}
                onFocus={setFocus}
                suggest={suggest}
                onAdd={addTask}
              />
            </section>
            <section className="levi-pool-card" aria-labelledby="pool-episodes">
              <h2 id="pool-episodes">
                {t("Episodes")}
                {focus && (
                  <>
                    {" · "}
                    <span className="levi-pool-muted">{focus}</span>{" "}
                    <button
                      type="button"
                      className="levi-pool-link"
                      onClick={() => setFocus(null)}
                    >
                      {t("Show all tasks")}
                    </button>
                  </>
                )}
              </h2>
              <EpisodeTable
                rows={episodes.episodes}
                total={episodes.total}
                offset={offset}
                pageSize={PAGE}
                chosenTasks={chosenNames}
                exclude={composition.exclude}
                onPage={setOffset}
                onToggleExclude={(key) =>
                  setComposition((c) => ({
                    ...c,
                    exclude: c.exclude.includes(key)
                      ? c.exclude.filter((k) => k !== key)
                      : [...c.exclude, key],
                  }))
                }
              />
            </section>
          </div>
          <div className="levi-pool-side">
            <CompositionPanel
              recipe={recipe}
              preview={preview}
              previewError={previewError}
              previewing={previewing}
              recipes={recipes}
              onChange={setComposition}
              onListEpisodes={listEpisodes}
              refreshKey={previewKey}
              onClear={() => setComposition(EMPTY_RECIPE)}
              onSave={() =>
                void act(async () => {
                  await leviRequest(
                    "PUT",
                    `pool/recipes/${encodeURIComponent(recipe.name)}`,
                    {
                      name: recipe.name,
                      categories: recipe.categories,
                      sources: recipe.sources,
                      tasks: recipe.tasks,
                      outcome: recipe.outcome,
                      per_task_cap: recipe.per_task_cap,
                      seed: recipe.seed,
                      date_from: recipe.date_from,
                      date_to: recipe.date_to,
                      policies: recipe.policies,
                      policy_models: recipe.policy_models || [],
                      policy_checkpoints: recipe.policy_checkpoints || [],
                      policy_methods: recipe.policy_methods || [],
                      include_nonstandard: recipe.include_nonstandard,
                      allow_unlinked_sources:
                        recipe.allow_unlinked_sources || false,
                      exclude: recipe.exclude,
                      task_text: recipe.task_text || {},
                    },
                  );
                  await refreshRecipes();
                  setNotice(`${t("Saved recipe")} ${recipe.name}`);
                })
              }
              onLoad={(name) =>
                void act(async () => {
                  const loaded = await leviRequest<Recipe>(
                    "GET",
                    `pool/recipes/${encodeURIComponent(name)}`,
                  );
                  setComposition({ ...EMPTY_RECIPE, ...loaded });
                  setFilters({
                    ...EMPTY_FILTERS,
                    categories: loaded.categories,
                    sources: loaded.sources,
                    policies: loaded.policies || [],
                    policyModels: loaded.policy_models || [],
                    policyCheckpoints: loaded.policy_checkpoints || [],
                    policyMethods: loaded.policy_methods || [],
                    outcome: loaded.outcome,
                    dateFrom: loaded.date_from || "",
                    dateTo: loaded.date_to || "",
                    showArchive: loaded.categories.includes("archive"),
                  });
                  setNotice(`${t("Loaded recipe")} ${name}`);
                })
              }
              onDelete={(name) =>
                void act(async () => {
                  await leviRequest(
                    "DELETE",
                    `pool/recipes/${encodeURIComponent(name)}`,
                  );
                  await refreshRecipes();
                  setNotice(`${t("Deleted recipe")} ${name}`);
                })
              }
            />
            <ExportPanel
              recipe={recipe}
              preview={preview}
              exportRoots={status?.export_roots || []}
              job={exportJob}
              humanAsSuccess={humanAsSuccess}
              onHumanAsSuccess={setHumanAsSuccess}
              format={format}
              onFormat={(next) => {
                setFormat(next);
                setTiming(null);
              }}
              fps={fps}
              onFps={setFps}
              timing={timing}
              onTiming={setTiming}
              onJob={(j) => {
                setExportJob(j);
                void refreshStatus();
              }}
              onPush={setPushFor}
            />
          </div>
        </div>
      )}
      <p className="levi-pool-hint">
        <Link className="text-cyan-300" href="/workbench">
          ← {t("Conversion & review")}
        </Link>
      </p>
      <PushDialog exportJob={pushFor} onClose={() => setPushFor(null)} />
    </main>
  );
}
