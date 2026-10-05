"use client";
import "@/components/pages-ui/pages.css";
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
import { RecentJobs } from "@/components/pool/job-list";
import { CleanupPanel, DiskUsage } from "@/components/pool/cleanup-panel";
import { LogDialog, useJobPolling } from "@/components/pool/job-panel";
import {
  PoolJobProgress,
  RUNNING,
  STOPPED,
  StatusBadge,
} from "@/components/pool/pool-progress";
import { ArrowLeft, Layers, PackagePlus, ScanSearch, X } from "lucide-react";
import { PoolSteps, goToSection, poolStep } from "@/components/pool/pool-steps";
import { Button, EmptyState, Icon, Skeleton, useToast } from "@/components/ds";
import { JobCard, RequestProblem } from "@/components/pages-ui/feedback";
import { defaultTiming } from "@/components/pool/types";
import { isLiveWorkspace } from "@/components/live/embedding";
import { useLivePulse } from "@/components/live/use-live-pulse";
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
  grippers: [],
  allow_mixed_gripper: false,
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
  for (const g of filters.grippers) q.append("gripper", g);
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

/** The live workspace's own LEVI (`levi live start --ui`) offers no training
 * pool: its pool would read its own workspace's index, recipes and labels,
 * not the product LEVI's, and two different pools confuse (docs/LIVE.md). */
export default function TrainingPoolPage() {
  const { t } = useLocale();
  const { enabled, embedded } = useLivePulse();
  if (isLiveWorkspace(enabled, embedded)) {
    return (
      <main className="ds-root pg-workbench">
        <div role="status">
          <EmptyState
            icon={Layers}
            title={t("The training pool is in the product LEVI")}
            description={t(
              "This is the live annotation service's own workspace. Its pool would read this workspace's own index, recipes and labels, not the ones you work with, so it is not offered here: open the training pool in the product LEVI (by default http://127.0.0.1:7860/pool).",
            )}
          />
        </div>
      </main>
    );
  }
  // Shown at once even before the live status is known: only the live
  // workspace's own LEVI replaces it.
  return <TrainingPool />;
}

function TrainingPool() {
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
  const [logFor, setLogFor] = useState<PoolJob | null>(null);
  // Bumped when jobs or files change, so the cleanup section reloads.
  const [cleanupKey, setCleanupKey] = useState(0);
  const [error, setError] = useState("");
  // Results of actions far from where they show (saved recipe, cleared
  // jobs, freed space) are toasts.
  const toast = useToast();
  const setNotice = useCallback(
    (text: string) => {
      if (text) toast.show({ title: text });
    },
    [toast],
  );

  const recipe: Recipe = useMemo(
    () => ({
      ...composition,
      categories: filters.categories,
      sources: filters.sources,
      policies: filters.policies,
      policy_models: filters.policyModels,
      policy_checkpoints: filters.policyCheckpoints,
      policy_methods: filters.policyMethods,
      grippers: filters.grippers,
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

  // The export the export panel follows: the one started or resumed here,
  // else a running, interrupted or failed one found in the job list (so a
  // reload during an export, or a worker that died, shows itself).
  const shownExport = useMemo(
    () =>
      exportJob ??
      status?.jobs.find(
        (j) =>
          j.kind === "export" &&
          (RUNNING.has(j.status) || STOPPED.has(j.status)),
      ) ??
      null,
    [exportJob, status],
  );
  // Follow it at the pace its state calls for.
  useJobPolling(shownExport, setExportJob);
  // A record that was cleared or deleted no longer belongs on the panel.
  useEffect(() => {
    if (
      exportJob &&
      status &&
      !RUNNING.has(exportJob.status) &&
      !status.jobs.some((j) => j.id === exportJob.id)
    )
      setExportJob(null);
  }, [status, exportJob]);

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
    <main className="ds-root pg-workbench pg-pool">
      <div className="pg-head">
        <h1>{t("Training pool")}</h1>
        <div className="pg-head-actions">
          {!scanned && (
            <span id="pool-export-why" className="pg-pool-muted pg-head-why">
              {t(
                status
                  ? "Scan the pool first to enable the export."
                  : "Export is available once the pool has been read.",
              )}
            </span>
          )}
          <Button
            variant={scanned ? "primary" : "secondary"}
            icon={PackagePlus}
            disabled={!scanned}
            aria-describedby={scanned ? undefined : "pool-export-why"}
            onClick={() => goToSection("pool-export", "pool-export-name")}
          >
            {t("Export…")}
          </Button>
        </div>
      </div>
      <p>
        {t(
          "Every dataset under the pool folders, one row per episode. Pick tasks in order, preview what goes in, export a new training set and send it to a training machine. Sources stay read-only; held-out test episodes are never exported.",
        )}
      </p>
      {error && (
        <RequestProblem
          action="The training pool request failed"
          message={error}
          live={false}
          onRetry={() => {
            setError("");
            refreshStatus().catch((e) => setError(String(e)));
            refreshRecipes().catch(() => {});
          }}
        />
      )}
      <PoolWarnings warnings={status?.warnings || []} />

      <section className="pg-box pg-pool-scan" aria-labelledby="pool-scan">
        <div className="pg-pool-scan-head">
          <div>
            <h2 id="pool-scan">{t("Pool folders")}</h2>
            {status && !status.enabled ? (
              <p id="pool-scan-why">
                {t(
                  "The training pool is idle: set LEVI_POOL_ROOTS to the folders it may read, then restart LEVI.",
                )}
              </p>
            ) : (
              <ul className="pg-pool-roots">
                {status?.roots.map((r) => (
                  <li key={r}>
                    <code>{r}</code>
                  </li>
                ))}
              </ul>
            )}
            <p className="pg-pool-hint">
              {t("Last scan")}:{" "}
              {summary ? when(summary.scanned_at) : status ? t("never") : "—"}
              {summary &&
                ` · ${summary.episodes.toLocaleString()} ${t("episodes")} · ${summary.sources.toLocaleString()} ${t("sources")} · ${summary.tasks.toLocaleString()} ${t("Tasks").toLowerCase()}`}
              {status?.heldout_lists.length
                ? ` · ${t("Held-out lists")}: ${status.heldout_lists.length}`
                : ""}
            </p>
          </div>
          <div className="pg-row">
            <Button
              variant={scanned || !status?.enabled ? "secondary" : "primary"}
              icon={ScanSearch}
              loading={scanRunning}
              disabled={!status?.enabled}
              aria-describedby={
                status && !status.enabled ? "pool-scan-why" : undefined
              }
              onClick={() =>
                void act(async () => {
                  await leviRequest("POST", "pool/scan");
                  await refreshStatus();
                })
              }
            >
              {scanRunning ? t("Scanning…") : t("Scan now")}
            </Button>
            {scanRunning && scanJob && (
              <Button
                icon={X}
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
              </Button>
            )}
          </div>
        </div>
        {scanJob && RUNNING.has(scanJob.status) && (
          <JobCard
            label={t("Scan")}
            status={<StatusBadge status={scanJob.status} />}
            title={t("Scanning…")}
          >
            <PoolJobProgress job={scanJob} />
          </JobCard>
        )}
        {status && status.jobs.length > 0 && (
          <details className="pg-pool-jobs">
            <summary>
              {t("Recent jobs")} ({status.jobs.length})
            </summary>
            <p className="pg-pool-hint">
              {t(
                "Running jobs are also listed in the Jobs menu of the top bar.",
              )}
            </p>
            <RecentJobs
              jobs={status.jobs}
              onChanged={() => {
                void refreshStatus();
                setCleanupKey((k) => k + 1);
              }}
              onPush={setPushFor}
              onJob={(j) => {
                if (j.kind === "export") setExportJob(j);
              }}
              onLog={setLogFor}
              onNotice={setNotice}
            />
          </details>
        )}
        <DiskUsage disk={status?.disk || []} />
      </section>

      {!scanned && status?.enabled && !scanRunning && (
        <EmptyState
          icon={ScanSearch}
          title={t("The pool has not been scanned yet: press Scan now.")}
        />
      )}
      {!status && !error && (
        <div className="pg-pool-loading" aria-busy="true">
          <span className="sr-only">{t("Loading…")}</span>
          <Skeleton height={120} radius="md" />
        </div>
      )}

      {scanned && (
        <PoolSteps
          current={poolStep(composition.tasks.length, !!shownExport)}
        />
      )}
      {scanned && (
        <div className="pg-pool-layout">
          <FacetsPanel
            facets={facets}
            filters={filters}
            onChange={setFilters}
          />
          <div className="pg-pool-centre">
            <section className="pg-pool-card" aria-labelledby="pool-tasks">
              <h2 id="pool-tasks">
                {t("Tasks")}{" "}
                <span className="pg-pool-muted">({tasks.length})</span>
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
            <section className="pg-pool-card" aria-labelledby="pool-episodes">
              <h2 id="pool-episodes">
                {t("Episodes")}
                {focus && (
                  <>
                    {" · "}
                    <span className="pg-pool-muted">{focus}</span>{" "}
                    <Button
                      size="sm"
                      variant="ghost"
                      icon={X}
                      onClick={() => setFocus(null)}
                    >
                      {t("Show all tasks")}
                    </Button>
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
          <div className="pg-pool-side">
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
                      robots: recipe.robots || [],
                      grippers: recipe.grippers || [],
                      allow_mixed_gripper: recipe.allow_mixed_gripper || false,
                      include_nonstandard: recipe.include_nonstandard,
                      allow_unlinked_sources:
                        recipe.allow_unlinked_sources || false,
                      exclude: recipe.exclude,
                      task_text: recipe.task_text || {},
                      task_corrections: recipe.task_corrections || [],
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
                    grippers: loaded.grippers || [],
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
              job={shownExport}
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
      {status?.enabled && (
        <CleanupPanel
          refreshKey={cleanupKey}
          onChanged={() => void refreshStatus()}
          onNotice={setNotice}
        />
      )}
      <p className="pg-pool-hint">
        <Link href="/workbench" className="pg-back-link">
          <Icon icon={ArrowLeft} />
          {t("Conversion & review")}
        </Link>
      </p>
      <PushDialog exportJob={pushFor} onClose={() => setPushFor(null)} />
      <LogDialog job={logFor} onClose={() => setLogFor(null)} />
    </main>
  );
}
