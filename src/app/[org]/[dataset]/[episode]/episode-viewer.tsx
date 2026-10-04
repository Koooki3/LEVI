// Modified for LEVI (2026); see NOTICE and docs/UPSTREAM.md.
"use client";
import { T, useLocale } from "@/components/levi-locale";
import {
  Box,
  ChartColumn,
  ChartSpline,
  ChevronDown,
  ChevronUp,
  Film,
  LayoutGrid,
  MessageSquareText,
  ScanSearch,
  Tags,
} from "lucide-react";
import { IconButton, Kbd, Tabs } from "@/components/ds";
import { AnalysisTab } from "@/components/viewer/analysis-tab";
import { EpisodeLoadError } from "@/components/viewer/load-error";
import {
  adjacentEpisode,
  loadsFor,
  restoreViewerTab,
  showsEpisodeList,
  type AnalysisView,
  type ViewerTab,
} from "@/components/viewer/viewer-tabs";
import "@/components/viewer/viewer.css";
import "@/components/viewer/annotations.css";

import {
  useState,
  useEffect,
  useCallback,
  useMemo,
  useRef,
  lazy,
  Suspense,
} from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { postParentMessageWithParams } from "@/utils/postParentMessage";
import { SimpleVideosPlayer } from "@/components/simple-videos-player";
import PlaybackBar from "@/components/playback-bar";
import { TimeProvider, useTime } from "@/context/time-context";
import { FlaggedEpisodesProvider } from "@/context/flagged-episodes-context";
import { DatasetSourceProvider } from "@/context/dataset-source-context";
import { RawCaptureNotice } from "@/components/raw-capture-notice";
import { DatasetUpdateNotice } from "@/components/dataset-update-notice";
import {
  AnnotationsProvider,
  useAnnotations,
} from "@/context/annotations-context";
import AnnotationRecorder from "@/components/annotation-recorder";
import { AnnotationsPanel } from "@/components/annotations-panel";
import ObjectAnnotationPanel from "@/components/object-annotation-panel";
import FastSegmentationPanel from "@/components/fast-segmentation-panel";
import { AnnotationsTimeline } from "@/components/annotations-timeline";
import Sidebar from "@/components/side-nav";
import StatsPanel from "@/components/stats-panel";
import OverviewPanel from "@/components/overview-panel";
import Loading from "@/components/loading-component";
import LeviDoctor from "@/components/levi-doctor";
import LeviReview from "@/components/levi-review";
import HfAuthButton from "@/components/hf-auth-button";
import { hasURDFSupport } from "@/lib/so101-robot";
import {
  getAdjacentEpisodesVideoInfo,
  computeColumnMinMax,
  getEpisodeDataSafe,
  loadAllEpisodeLengthsV3,
  loadAllEpisodeFrameInfo,
  loadCrossEpisodeActionVariance,
  loadDatasetTaskIndex,
  loadEpisodeOutcomes,
  CROSS_EPISODE_DEFAULTS,
  type EpisodeData,
  type ColumnMinMax,
  type EpisodeLengthStats,
  type EpisodeFramesData,
  type CrossEpisodeVarianceData,
  type CrossEpisodeRequest,
  type DatasetTaskIndex,
  type EpisodeOutcome,
} from "./fetch-data";
import { getDatasetVersionAndInfo, isDatasetV3 } from "@/utils/versionUtils";
import {
  readBrowserStorage,
  removeBrowserStorage,
  writeBrowserStorage,
} from "@/utils/browserStorage";
import type { DatasetMetadata } from "@/utils/parquetUtils";
import { urlSeekTarget, urlTimeWriter } from "@/utils/urlTime";
import {
  fetchAnnotationSummary,
  fetchOutcomeLabels,
  fetchRecapSummary,
  isAnnotateBackendEnabled,
  saveOutcomeLabel,
  type AnnotationSummary,
} from "@/utils/annotationsClient";

import {
  OUTCOME_LABELS_CHANGED_EVENT,
  RECAP_UPDATED_EVENT,
} from "@/types/recap.types";

const URDFViewer = lazy(() => import("@/components/urdf-viewer"));
const ActionInsightsPanel = lazy(
  () => import("@/components/action-insights-panel"),
);
const FilteringPanel = lazy(() => import("@/components/filtering-panel"));
// Recharts is ~150KB gz and not above-the-fold (videos render first on the
// Episodes tab). Lazy-load it so the initial chunk can ship faster and
// videos start downloading in parallel with the chart bundle.
const DataRecharts = lazy(() => import("@/components/data-recharts"));

/** Skip global playback / navigation shortcuts while typing in a field. */
function isKeyboardFocusInsideTextEntry(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target.isContentEditable || target.closest('[contenteditable="true"]')) {
    return true;
  }
  const tag = target.tagName;
  return (
    tag === "TEXTAREA" ||
    tag === "SELECT" ||
    tag === "INPUT" ||
    tag === "BUTTON" ||
    (tag === "A" && target.hasAttribute("href"))
  );
}

// Subscribes to `currentTime` so its parent doesn't have to. Keeping this
// in a leaf component means the throttled time ticks (~12.5/s during
// playback) only re-render this no-op sub-tree, not the entire 700-line
// EpisodeViewerInner. Vercel rule: rerender-defer-reads.
function UrlTimeSync() {
  const { currentTime, isPlaying } = useTime();
  const searchParams = useSearchParams();
  const lastUrlSecondRef = useRef<number>(-1);

  // Only update the URL ?t= param when the integer second changes, and
  // only while paused — replacing state every frame during playback would
  // spam the browser's history.
  useEffect(() => {
    if (isPlaying) return;
    const currentSec = Math.floor(currentTime);
    if (currentTime > 0 && lastUrlSecondRef.current !== currentSec) {
      lastUrlSecondRef.current = currentSec;
      const newParams = new URLSearchParams(searchParams.toString());
      newParams.set("t", currentSec.toString());
      // Next.js reports this replaceState through useSearchParams; the
      // ?t= seek below must not snap the playhead back to the whole second.
      urlTimeWriter.last = currentSec.toString();
      window.history.replaceState(
        {},
        "",
        `${window.location.pathname}?${newParams.toString()}`,
      );
      postParentMessageWithParams((params: URLSearchParams) => {
        params.set("path", window.location.pathname + window.location.search);
      });
    }
  }, [isPlaying, currentTime, searchParams]);

  return null;
}

export default function EpisodeViewer({
  org,
  dataset,
  episodeId,
}: {
  org: string;
  dataset: string;
  episodeId: number;
}) {
  const [data, setData] = useState<EpisodeData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const requestIdRef = useRef(0);
  const [authRevision, setAuthRevision] = useState(0);
  const legacyRouter = useRouter();
  // Local datasets were once addressed by a hash (`/local/<hash>`); the
  // catalog keeps those as aliases of the current name. Redirect, carrying
  // this browser's flagged episodes over once.
  useEffect(() => {
    if (org !== "local") return;
    let cancelled = false;
    fetch("/api/levi/catalog", { cache: "no-store" })
      .then((response) => (response.ok ? response.json() : null))
      .then((catalog: { aliases?: Record<string, string> } | null) => {
        const target = catalog?.aliases?.[dataset];
        if (cancelled || !target || target === dataset) return;
        const oldKey = `levi-flags:local/${dataset}`;
        const newKey = `levi-flags:local/${target}`;
        const flags = readBrowserStorage("local", oldKey);
        if (flags && !readBrowserStorage("local", newKey))
          writeBrowserStorage("local", newKey, flags);
        legacyRouter.replace(
          `/local/${target}/episode_${episodeId}${window.location.search}`,
        );
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [org, dataset, episodeId, legacyRouter]);
  useEffect(() => {
    const onAuthChanged = () => setAuthRevision((value) => value + 1);
    window.addEventListener("levi:hf-auth-changed", onAuthChanged);
    return () =>
      window.removeEventListener("levi:hf-auth-changed", onAuthChanged);
  }, []);

  useEffect(() => {
    if (Number.isNaN(episodeId)) {
      setError("Invalid episode id.");
      setData(null);
      return;
    }
    const requestId = ++requestIdRef.current;
    setError(null);
    setData(null);
    getEpisodeDataSafe(org, dataset, episodeId)
      .then(({ data: loaded, error: loadError }) => {
        if (requestIdRef.current !== requestId) return;
        if (loadError) {
          setError(loadError);
          setData(null);
          return;
        }
        setData(loaded ?? null);
      })
      .catch((err) => {
        if (requestIdRef.current !== requestId) return;
        const message = err instanceof Error ? err.message : String(err);
        setError(message || "Unknown error");
        setData(null);
      });
  }, [org, dataset, episodeId, authRevision]);

  if (error) {
    return (
      <EpisodeLoadError
        message={error}
        onRetry={() => setAuthRevision((value) => value + 1)}
      />
    );
  }

  if (!data) {
    return (
      <div className="vw-root ds-root relative">
        <Loading />
      </div>
    );
  }

  return (
    <T>
      {
        <TimeProvider duration={data!.duration}>
          <DatasetSourceProvider org={org} dataset={dataset}>
            <FlaggedEpisodesProvider
              key={`${org}/${dataset}`}
              repoId={`${org}/${dataset}`}
            >
              <AnnotationsProvider>
                <EpisodeBootstrap data={data!} />
                <EpisodeViewerInner data={data!} org={org} dataset={dataset} />
                <DatasetUpdateNotice />
              </AnnotationsProvider>
            </FlaggedEpisodesProvider>
          </DatasetSourceProvider>
        </TimeProvider>
      }
    </T>
  );
}

/** Wires the loaded episode into the AnnotationsProvider. */
function EpisodeBootstrap({ data }: { data: EpisodeData }) {
  const { setEpisode } = useAnnotations();
  useEffect(() => {
    setEpisode(
      data.episodeId,
      { repoId: data.datasetInfo.repoId },
      data.languageAtoms,
      data.frameTimestamps,
    );
  }, [
    data.episodeId,
    data.datasetInfo.repoId,
    data.languageAtoms,
    data.frameTimestamps,
    setEpisode,
  ]);
  return null;
}

function EpisodeViewerInner({
  data,
  org,
  dataset,
}: {
  data: EpisodeData;
  org?: string;
  dataset?: string;
}) {
  const {
    datasetInfo,
    episodeId,
    videosInfo,
    chartDataGroups,
    episodes,
    task,
  } = data;

  const { t } = useLocale();
  const [videosReady, setVideosReady] = useState(!videosInfo.length);
  const [chartsReady, setChartsReady] = useState(false);

  const loadStartRef = useRef(performance.now());

  const router = useRouter();
  const searchParams = useSearchParams();

  // Tab state & lazy stats — read sessionStorage in the initializer so the
  // correct tab renders on the very first frame (no post-mount flash).
  // Safe because EpisodeViewerInner only mounts client-side (behind a loading gate).
  // "Action insights", "Filtering" and "Doctor" are views of one "Analysis"
  // tab; an old stored tab id opens that view (restoreViewerTab).
  const [initialTabs] = useState(() =>
    typeof window !== "undefined"
      ? restoreViewerTab(
          readBrowserStorage("session", "activeTab"),
          readBrowserStorage("session", "analysisView"),
        )
      : restoreViewerTab(null, null),
  );
  const [activeTab, setActiveTab] = useState<ViewerTab>(initialTabs.tab);
  const [analysisView, setAnalysisView] = useState<AnalysisView>(
    initialTabs.view,
  );
  // Sub-tab within "Annotations": language/event annotation is a fully
  // decoupled system from SAM3 object/track/mask annotation. sessionStorage
  // (not local-only state) because episode navigation goes through
  // Next.js's dynamic `[episode]` route segment, which remounts this whole
  // component on every episode switch (confirmed: a lazy-init random id
  // stamped here differs before/after a same-dataset episode navigation) —
  // plain useState would silently reset every time you switch episodes.
  // Same reasoning and pattern as `activeTab` above.
  const [annotationsSubTab, setAnnotationsSubTab] = useState<
    "language" | "vision"
  >(() => {
    if (typeof window !== "undefined") {
      const stored = readBrowserStorage("session", "annotationsSubTab");
      if (stored === "language" || stored === "vision") return stored;
    }
    return "language";
  });
  const isLoading = activeTab === "episodes" && (!videosReady || !chartsReady);

  useEffect(() => {
    if (!isLoading) {
      console.log(
        `[perf] Loading complete in ${(performance.now() - loadStartRef.current).toFixed(0)}ms (videos: ${videosReady ? "✓" : "…"}, charts: ${chartsReady ? "✓" : "…"})`,
      );
    }
  }, [isLoading, videosReady, chartsReady]);
  const [, setColumnMinMax] = useState<ColumnMinMax[] | null>(null);
  const [episodeLengthStats, setEpisodeLengthStats] =
    useState<EpisodeLengthStats | null>(null);
  const [statsLoading, setStatsLoading] = useState(false);
  const statsLoadedRef = useRef(false);
  const [episodeFramesData, setEpisodeFramesData] =
    useState<EpisodeFramesData | null>(null);
  const [framesLoading, setFramesLoading] = useState(false);
  const framesLoadedRef = useRef(false);
  const [framesFlaggedOnly, setFramesFlaggedOnly] = useState(() =>
    typeof window !== "undefined"
      ? readBrowserStorage("session", "framesFlaggedOnly") === "true"
      : false,
  );
  const [sidebarFlaggedOnly, setSidebarFlaggedOnly] = useState(() =>
    typeof window !== "undefined"
      ? readBrowserStorage("session", "sidebarFlaggedOnly") === "true"
      : false,
  );
  const [sidebarFailuresOnly, setSidebarFailuresOnly] = useState(() =>
    typeof window !== "undefined"
      ? readBrowserStorage("session", "sidebarFailuresOnly") === "true"
      : false,
  );
  const [crossEpData, setCrossEpData] =
    useState<CrossEpisodeVarianceData | null>(null);
  const [insightsLoading, setInsightsLoading] = useState(false);
  const [insightsRequest, setInsightsRequest] = useState<CrossEpisodeRequest>({
    scope: { kind: "all" },
    maxEpisodes: CROSS_EPISODE_DEFAULTS.sampleSize,
  });
  const [insightsProgress, setInsightsProgress] = useState<{
    loaded: number;
    total: number;
  } | null>(null);
  const [taskIndexLoaded, setTaskIndexLoaded] = useState(false);
  // Key of the request already loaded (or in flight), so revisiting the tab
  // doesn't refetch but changing the scope does.
  const insightsLoadedRef = useRef<string | null>(null);
  // Only the newest request may write state — a wide scope started first must
  // not overwrite a narrow one the user asked for afterwards.
  const insightsRunRef = useRef(0);
  const [taskIndex, setTaskIndex] = useState<DatasetTaskIndex | null>(null);
  // sessionStorage-backed for the same reason as annotationsSubTab above —
  // this component remounts on every episode navigation. Scoped by dataset
  // (unlike activeTab/annotationsSubTab, which are pure UI preferences):
  // a task name from one dataset is meaningless — possibly not even a valid
  // option — in another, so a filter shouldn't leak across datasets.
  const [taskFilter, setTaskFilter] = useState<string | null>(() => {
    if (typeof window !== "undefined") {
      return readBrowserStorage("session", `taskFilter:${org}/${dataset}`);
    }
    return null;
  });
  const [annotationSummary, setAnnotationSummary] =
    useState<AnnotationSummary | null>(null);
  const [episodeOutcomes, setEpisodeOutcomes] = useState<Record<
    string,
    EpisodeOutcome
  > | null>(null);
  // Human labels (annotation backend) layered over the metadata outcomes.
  const [humanOutcomes, setHumanOutcomes] = useState<Record<
    string,
    EpisodeOutcome
  > | null>(null);
  const mergedOutcomes = useMemo(
    () =>
      episodeOutcomes || humanOutcomes
        ? { ...(episodeOutcomes ?? {}), ...(humanOutcomes ?? {}) }
        : null,
    [episodeOutcomes, humanOutcomes],
  );
  // RECAP value model: per-episode share of positive-advantage frames, for
  // the sidebar badge. Absent (null) until advantage labels exist.
  const [recapFractions, setRecapFractions] = useState<Record<
    string,
    number
  > | null>(null);
  const humanOutcomeKeys = useMemo(
    () => new Set(Object.keys(humanOutcomes ?? {})),
    [humanOutcomes],
  );
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    statsLoadedRef.current = false;
    framesLoadedRef.current = false;
    insightsLoadedRef.current = null;
    setEpisodeLengthStats(null);
    setEpisodeFramesData(null);
    setCrossEpData(null);
    setTaskIndex(null);
    setAnnotationSummary(null);
    // taskFilter reset moved to the outer EpisodeViewer, keyed on org/dataset
    // instead of datasetInfo.repoId: this effect lives inside a component
    // that fully remounts on every episode navigation (see EpisodeViewer's
    // fetch effect), so a `datasetInfo.repoId` dependency looks "new" on
    // every single remount, not just on a genuine dataset switch — it would
    // silently null the now-lifted taskFilter right after every episode
    // change if it stayed here.
  }, [datasetInfo.repoId]);

  // Task metadata drives both the sidebar filter and the by-task insights
  // scope, so load it once per dataset rather than per panel.
  useEffect(() => {
    if (!org || !dataset) return;
    setTaskIndexLoaded(false);
    const repoId = `${org}/${dataset}`;
    let cancelled = false;
    getDatasetVersionAndInfo(repoId)
      .then(({ version, info }) =>
        loadDatasetTaskIndex(
          repoId,
          version,
          info as unknown as DatasetMetadata,
        ),
      )
      .then((result) => {
        if (!cancelled && mountedRef.current) setTaskIndex(result);
        if (!cancelled) setTaskIndexLoaded(true);
      })
      .catch(() => {
        if (!cancelled) setTaskIndexLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, [org, dataset]);

  // Sidebar's per-episode annotated/unannotated indicator. No-op without an
  // annotation backend configured.
  useEffect(() => {
    if (!org || !dataset || !isAnnotateBackendEnabled()) return;
    let cancelled = false;
    fetchAnnotationSummary({ repoId: `${org}/${dataset}` })
      .then((result) => {
        if (!cancelled && mountedRef.current) setAnnotationSummary(result);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [org, dataset]);

  // Sidebar's per-episode success/failure badge — dataset-native metadata
  // (from meta/episodes.jsonl, written by LEVI's converter for policy-eval
  // rollout captures), not a LEVI annotation sidecar, so no backend check.
  useEffect(() => {
    if (!org || !dataset) return;
    const repoId = `${org}/${dataset}`;
    let cancelled = false;
    getDatasetVersionAndInfo(repoId)
      .then(({ version }) => loadEpisodeOutcomes(repoId, version))
      .then((result) => {
        if (!cancelled && mountedRef.current) setEpisodeOutcomes(result);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [org, dataset]);

  useEffect(() => {
    if (!org || !dataset || !isAnnotateBackendEnabled()) return;
    let cancelled = false;
    fetchOutcomeLabels({ repoId: `${org}/${dataset}` })
      .then((labels) => {
        if (cancelled || !mountedRef.current) return;
        setHumanOutcomes(
          Object.fromEntries(
            Object.entries(labels).map(([ep, label]) => [ep, label.outcome]),
          ),
        );
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [org, dataset]);

  // Refetched when a run in the timeline's VALUE MODEL section finishes.
  useEffect(() => {
    if (!org || !dataset || !isAnnotateBackendEnabled()) return;
    let controller = new AbortController();
    const load = () => {
      controller.abort();
      controller = new AbortController();
      fetchRecapSummary({ repoId: `${org}/${dataset}` }, controller.signal)
        .then((summary) => {
          if (!mountedRef.current) return;
          setRecapFractions(
            summary
              ? Object.fromEntries(
                  Object.entries(summary.episodes).map(([ep, row]) => [
                    ep,
                    row.positive_fraction,
                  ]),
                )
              : null,
          );
        })
        .catch(() => {});
    };
    load();
    window.addEventListener(RECAP_UPDATED_EVENT, load);
    return () => {
      controller.abort();
      window.removeEventListener(RECAP_UPDATED_EVENT, load);
    };
  }, [org, dataset]);

  const changeOutcome = useCallback(
    (episode: number, outcome: EpisodeOutcome | null) => {
      const key = String(episode);
      const previous = humanOutcomes;
      // Optimistic; restored if the save fails.
      setHumanOutcomes((current) => {
        const next = { ...(current ?? {}) };
        if (outcome) next[key] = outcome;
        else delete next[key];
        return next;
      });
      saveOutcomeLabel(episode, { repoId: `${org}/${dataset}` }, outcome)
        // Advantage labels computed before this are now stale.
        .then(() =>
          window.dispatchEvent(new CustomEvent(OUTCOME_LABELS_CHANGED_EVENT)),
        )
        .catch(() => {
          if (mountedRef.current) setHumanOutcomes(previous);
        });
    },
    [org, dataset, humanOutcomes],
  );

  // Eagerly load the URDFViewer bundle + warm the STL geometry cache while
  // the user is on the Episodes tab, so the 3D Replay tab opens faster.
  useEffect(() => {
    if (
      hasURDFSupport(datasetInfo.robot_type) &&
      isDatasetV3(datasetInfo.codebase_version)
    ) {
      void import("@/components/urdf-viewer");
    }
  }, [datasetInfo.robot_type, datasetInfo.codebase_version]);

  // Persist UI state across episode navigations. One effect instead of
  // several near-identical writes — fewer commit hooks per render and the
  // intent (mirror these primitives to sessionStorage) reads as one unit.
  // Needed because this component remounts on every episode navigation
  // (Next.js recreates the subtree under a dynamic `[episode]` route
  // segment on every param change) — without this, plain useState for any
  // of these would silently reset on every episode switch.
  useEffect(() => {
    writeBrowserStorage("session", "activeTab", activeTab);
    writeBrowserStorage("session", "analysisView", analysisView);
    writeBrowserStorage(
      "session",
      "sidebarFlaggedOnly",
      String(sidebarFlaggedOnly),
    );
    writeBrowserStorage(
      "session",
      "sidebarFailuresOnly",
      String(sidebarFailuresOnly),
    );
    writeBrowserStorage(
      "session",
      "framesFlaggedOnly",
      String(framesFlaggedOnly),
    );
    writeBrowserStorage("session", "annotationsSubTab", annotationsSubTab);
    const taskFilterKey = `taskFilter:${org}/${dataset}`;
    if (taskFilter) writeBrowserStorage("session", taskFilterKey, taskFilter);
    else removeBrowserStorage("session", taskFilterKey);
  }, [
    activeTab,
    analysisView,
    sidebarFlaggedOnly,
    sidebarFailuresOnly,
    framesFlaggedOnly,
    annotationsSubTab,
    taskFilter,
    org,
    dataset,
  ]);

  const loadStats = () => {
    if (statsLoadedRef.current) return;
    statsLoadedRef.current = true;
    setStatsLoading(true);
    setColumnMinMax(computeColumnMinMax(data.chartDataGroups));
    if (org && dataset) {
      const repoId = `${org}/${dataset}`;
      getDatasetVersionAndInfo(repoId)
        .then(({ version, info }) => {
          return loadAllEpisodeLengthsV3(repoId, version, info.fps);
        })
        .then((result) => {
          if (!mountedRef.current) return;
          setEpisodeLengthStats(result);
        })
        .catch(() => {})
        .finally(() => {
          if (mountedRef.current) setStatsLoading(false);
        });
    } else {
      setStatsLoading(false);
    }
  };

  const loadFrames = () => {
    if (framesLoadedRef.current || !org || !dataset) return;
    framesLoadedRef.current = true;
    setFramesLoading(true);
    const repoId = `${org}/${dataset}`;
    getDatasetVersionAndInfo(repoId)
      .then(({ version, info }) =>
        loadAllEpisodeFrameInfo(
          repoId,
          version,
          info as unknown as DatasetMetadata,
        ),
      )
      .then((result) => {
        if (!mountedRef.current) return;
        setEpisodeFramesData(result);
      })
      .catch(() => {
        if (!mountedRef.current) return;
        setEpisodeFramesData({ cameras: [], framesByCamera: {} });
      })
      .finally(() => {
        if (mountedRef.current) setFramesLoading(false);
      });
  };

  const loadInsights = (request: CrossEpisodeRequest = insightsRequest) => {
    if (!org || !dataset) return;
    const requestKey = JSON.stringify(request);
    if (insightsLoadedRef.current === requestKey) return;
    insightsLoadedRef.current = requestKey;
    const runId = ++insightsRunRef.current;
    setInsightsLoading(true);
    setInsightsProgress({ loaded: 0, total: 0 });
    const repoId = `${org}/${dataset}`;
    getDatasetVersionAndInfo(repoId)
      .then(({ version, info }) =>
        loadCrossEpisodeActionVariance(
          repoId,
          version,
          info as unknown as DatasetMetadata,
          info.fps,
          {
            ...request,
            onProgress: (loaded, total) => {
              if (mountedRef.current && insightsRunRef.current === runId) {
                setInsightsProgress({ loaded, total });
              }
            },
          },
        ),
      )
      .then((result) => {
        if (!mountedRef.current || insightsRunRef.current !== runId) return;
        setCrossEpData(result);
      })
      .catch((err) => {
        console.error("[cross-ep] Failed:", err);
        // Let the user retry the same scope after a failure.
        if (insightsLoadedRef.current === requestKey) {
          insightsLoadedRef.current = null;
        }
      })
      .finally(() => {
        if (!mountedRef.current || insightsRunRef.current !== runId) return;
        setInsightsLoading(false);
        setInsightsProgress(null);
      });
  };

  const applyInsightsRequest = (request: CrossEpisodeRequest) => {
    setInsightsRequest(request);
    loadInsights(request);
  };

  const loadFor = (tab: ViewerTab, view: AnalysisView) => {
    const needs = loadsFor(tab, view);
    if (needs.stats) loadStats();
    if (needs.frames) loadFrames();
    if (needs.insights) loadInsights();
  };

  // Re-trigger data loading for the restored tab on mount
  useEffect(() => {
    loadFor(activeTab, analysisView);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleTabChange = (tab: ViewerTab) => {
    setActiveTab(tab);
    loadFor(tab, analysisView);
  };

  const handleAnalysisViewChange = (view: AnalysisView) => {
    setAnalysisView(view);
    loadFor("analysis", view);
  };

  // `currentTime` is intentionally NOT read here. Subscribing to it would
  // re-render this 700-line component every ~80ms during playback. The
  // <UrlTimeSync /> child handles its only consumer (the ?t= URL writer).
  // `seek` and `setIsPlaying` are stable references from useCallback /
  // useState — they don't drive renders.
  const { seek, setIsPlaying } = useTime();
  useEffect(() => {
    const handler = (event: Event) => {
      const {
        repo_id: evidenceRepo,
        episode_index,
        timestamp,
        follow,
      } = (event as CustomEvent).detail;
      if (
        follow &&
        evidenceRepo === datasetInfo.repoId &&
        episode_index === episodeId &&
        Number.isFinite(timestamp)
      ) {
        setIsPlaying(false);
        seek(timestamp);
      }
    };
    window.addEventListener("levi-agent-seek", handler);
    return () => window.removeEventListener("levi-agent-seek", handler);
  }, [datasetInfo.repoId, episodeId, seek, setIsPlaying]);

  // URDFViewer episode changer and play toggle — populated by URDFViewer on mount
  const urdfChangerRef = useRef<((ep: number) => void) | undefined>(undefined);
  const urdfPlayToggleRef = useRef<(() => void) | undefined>(undefined);
  const [urdfEpisode, setUrdfEpisode] = useState(episodeId);
  useEffect(() => setUrdfEpisode(episodeId), [episodeId]);

  // Prefer episode indices observed in metadata when it is available. Some
  // exports have stale total_episodes values or non-contiguous IDs; keeping
  // the declared range as a fallback preserves browsing for datasets whose
  // metadata does not expose an episode table.
  const availableEpisodes = useMemo(() => {
    if (!taskIndex) return episodes;
    const observed = Object.keys(taskIndex.episodeTasks)
      .map(Number)
      .filter((episode) => Number.isInteger(episode) && episode >= 0);
    return [...new Set([...episodes, ...observed])].sort((a, b) => a - b);
  }, [episodes, taskIndex]);

  // Episode list, narrowed to the selected task on multi-task datasets.
  const visibleEpisodes = useMemo(() => {
    if (!taskFilter || !taskIndexLoaded) return availableEpisodes;
    if (!taskIndex) return [];
    return availableEpisodes.filter((ep) =>
      (taskIndex.episodeTasks[ep] ?? []).includes(taskFilter),
    );
  }, [availableEpisodes, taskFilter, taskIndex, taskIndexLoaded]);

  // A filter restored from sessionStorage can belong to an older dataset or a
  // renamed task. Clear it as soon as authoritative metadata arrives.
  useEffect(() => {
    if (
      taskFilter &&
      taskIndexLoaded &&
      (!taskIndex || !taskIndex.tasks.includes(taskFilter))
    ) {
      setTaskFilter(null);
    }
  }, [taskFilter, taskIndex, taskIndexLoaded]);

  // Keep the route and the selected task in sync. Without this, choosing a
  // task that excludes the current episode leaves the sidebar empty and the
  // arrow shortcuts continue to walk hidden episode IDs.
  useEffect(() => {
    if (
      taskFilter &&
      taskIndexLoaded &&
      visibleEpisodes.length > 0 &&
      !visibleEpisodes.includes(episodeId)
    ) {
      router.replace(`./episode_${visibleEpisodes[0]}`);
    }
  }, [
    episodeId,
    router,
    taskFilter,
    taskIndex,
    taskIndexLoaded,
    visibleEpisodes,
  ]);

  // Pagination state. Lazily computed from the CURRENT episode's position
  // (not just `useState(1)`) so a fresh mount — which happens on every
  // episode navigation, see the sessionStorage comment above — starts on
  // the right page immediately instead of flashing page 1 before the
  // correction effect below catches up. Doesn't need sessionStorage itself:
  // it's a pure derivation of already-available data, so there's nothing to
  // persist independently of that data.
  const pageSize = 100;
  const [currentPage, setCurrentPage] = useState(() => {
    const idx = visibleEpisodes.indexOf(episodeId);
    return idx === -1 ? 1 : Math.floor(idx / pageSize) + 1;
  });
  const totalPages = Math.max(1, Math.ceil(visibleEpisodes.length / pageSize));
  const paginatedEpisodes = visibleEpisodes.slice(
    (currentPage - 1) * pageSize,
    currentPage * pageSize,
  );

  // Preload adjacent episodes' videos via <link rel="preload"> tags
  useEffect(() => {
    if (!org || !dataset) return;
    const links: HTMLLinkElement[] = [];

    getAdjacentEpisodesVideoInfo(org, dataset, episodeId, 2)
      .then((adjacentVideos) => {
        for (const ep of adjacentVideos) {
          for (const v of ep.videosInfo) {
            const link = document.createElement("link");
            link.rel = "preload";
            link.as = "video";
            link.href = v.url;
            document.head.appendChild(link);
            links.push(link);
          }
        }
      })
      .catch(() => {});

    return () => {
      links.forEach((l) => l.remove());
    };
  }, [org, dataset, episodeId]);

  // Initialize based on URL time parameter
  useEffect(() => {
    const target = urlSeekTarget(searchParams.get("t"));
    if (target != null) seek(target);
  }, [searchParams, seek]);

  // sync with parent window hf.co/spaces
  useEffect(() => {
    postParentMessageWithParams((params: URLSearchParams) => {
      params.set("path", window.location.pathname + window.location.search);
    });
  }, []);

  // Initialize page based on the current episode. Splitting this out from
  // the keyboard listener effect lets the listener attach exactly once.
  // When a task filter hides the current episode, fall back to page 1.
  useEffect(() => {
    const episodeIndex = visibleEpisodes.indexOf(episodeId);
    setCurrentPage(
      episodeIndex === -1 ? 1 : Math.floor(episodeIndex / pageSize) + 1,
    );
  }, [visibleEpisodes, episodeId, pageSize, setCurrentPage]);

  // Mirror the values the keydown handler needs into a ref. Without this,
  // `useCallback` would produce a new handler whenever `activeTab` /
  // `episodeId` / `urdfEpisode` changed, and the keydown effect would
  // detach + reattach the listener each time. Now the listener attaches
  // once and reads the latest state via the ref.
  // Vercel rule: advanced-event-handler-refs.
  const keyStateRef = useRef({
    activeTab,
    episodeId,
    episodes,
    taskFilter,
    visibleEpisodes,
    urdfEpisode,
  });
  keyStateRef.current = {
    activeTab,
    episodeId,
    episodes,
    taskFilter,
    visibleEpisodes,
    urdfEpisode,
  };

  // ↑/↓ and the heading's previous/next buttons: one step through the
  // visible (task-filtered) episodes; on the 3D Replay tab the replay's own
  // episode changes instead of the route.
  const stepEpisodeRef = useRef<(delta: 1 | -1) => void>(() => {});
  stepEpisodeRef.current = (delta) => {
    const s = keyStateRef.current;
    if (s.taskFilter && s.visibleEpisodes.length === 0) return;
    const navigationEpisodes = s.taskFilter
      ? s.visibleEpisodes
      : s.visibleEpisodes.length > 0
        ? s.visibleEpisodes
        : s.episodes;
    const current = s.activeTab === "urdf" ? s.urdfEpisode : s.episodeId;
    const nextEp = adjacentEpisode(navigationEpisodes, current, delta);
    if (nextEp === undefined) return;
    if (s.activeTab === "urdf") {
      setUrdfEpisode(nextEp);
      urdfChangerRef.current?.(nextEp);
    } else {
      router.push(`./episode_${nextEp}`);
    }
  };

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      const { key } = e;
      const s = keyStateRef.current;
      const inTextEntry = isKeyboardFocusInsideTextEntry(e.target);

      if (key === " ") {
        if (inTextEntry) return;
        e.preventDefault();
        if (s.activeTab === "urdf") {
          urdfPlayToggleRef.current?.();
        } else {
          setIsPlaying((prev: boolean) => !prev);
        }
      } else if (key === "ArrowDown" || key === "ArrowUp") {
        if (inTextEntry) return;
        e.preventDefault();
        stepEpisodeRef.current(key === "ArrowDown" ? 1 : -1);
      }
    };

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
    // router / setIsPlaying are stable; the rest is read via keyStateRef.
  }, [router, setIsPlaying]);

  // Pagination functions
  const nextPage = () => {
    if (currentPage < totalPages) {
      setCurrentPage((prev) => prev + 1);
    }
  };

  const prevPage = () => {
    if (currentPage > 1) {
      setCurrentPage((prev) => prev - 1);
    }
  };

  const tabItems = [
    { id: "episodes", label: t("Episodes"), icon: Film },
    { id: "annotations", label: t("Annotations"), icon: Tags },
    ...(hasURDFSupport(datasetInfo.robot_type)
      ? [{ id: "urdf", label: t("3D Replay"), icon: Box }]
      : []),
    { id: "statistics", label: t("Statistics"), icon: ChartColumn },
    { id: "frames", label: t("Frame gallery"), icon: LayoutGrid },
    { id: "analysis", label: t("Analysis"), icon: ChartSpline },
  ];

  const navEpisodes = taskFilter
    ? visibleEpisodes
    : visibleEpisodes.length > 0
      ? visibleEpisodes
      : episodes;
  const shownEpisode = activeTab === "urdf" ? urdfEpisode : episodeId;
  const episodePosition = navEpisodes.indexOf(shownEpisode);

  const heading = (
    <div className="vw-heading">
      <div className="vw-heading-nav">
        <IconButton
          icon={ChevronUp}
          label={t("Previous episode")}
          shortcut="↑"
          size="sm"
          disabled={
            adjacentEpisode(navEpisodes, shownEpisode, -1) === undefined
          }
          onClick={() => stepEpisodeRef.current(-1)}
        />
        <IconButton
          icon={ChevronDown}
          label={t("Next episode")}
          shortcut="↓"
          size="sm"
          disabled={adjacentEpisode(navEpisodes, shownEpisode, 1) === undefined}
          onClick={() => stepEpisodeRef.current(1)}
        />
      </div>
      <div className="vw-heading-title">
        <h1>{t(`Episode ${episodeId}`)}</h1>
        <span className="vw-heading-meta">
          {org === "local" ? (
            datasetInfo.repoId
          ) : (
            <a
              href={`https://huggingface.co/datasets/${datasetInfo.repoId}`}
              target="_blank"
              rel="noreferrer"
              className="vw-link"
            >
              {datasetInfo.repoId}
            </a>
          )}
          {episodePosition >= 0 && (
            <>
              {" · "}
              {episodePosition + 1} / {navEpisodes.length}
            </>
          )}
        </span>
      </div>
      {activeTab === "annotations" && (
        <div className="vw-heading-actions">
          <AnnotationRecorder />
        </div>
      )}
    </div>
  );

  return (
    <div className="vw-root ds-root">
      <UrlTimeSync />
      {/* Top tab bar */}
      <div className="vw-tabbar">
        <Tabs
          label={t("Episode viewer")}
          items={tabItems}
          value={activeTab}
          onChange={(id) => handleTabChange(id as ViewerTab)}
        />
        <div className="vw-tabbar-actions">
          <LeviReview repoId={`${org}/${dataset}`} />
          <HfAuthButton variant="tab" />
        </div>
      </div>

      {/* Body: sidebar + content */}
      <div className="vw-body">
        {/* Episode list — on Episodes, Annotations and 3D Replay */}
        {showsEpisodeList(activeTab) && (
          <Sidebar
            datasetInfo={datasetInfo}
            paginatedEpisodes={paginatedEpisodes}
            allVisibleEpisodes={visibleEpisodes}
            episodeId={activeTab === "urdf" ? urdfEpisode : episodeId}
            totalPages={totalPages}
            currentPage={currentPage}
            prevPage={prevPage}
            nextPage={nextPage}
            showFlaggedOnly={sidebarFlaggedOnly}
            onShowFlaggedOnlyChange={setSidebarFlaggedOnly}
            showFailuresOnly={sidebarFailuresOnly}
            onShowFailuresOnlyChange={setSidebarFailuresOnly}
            tasks={taskIndex?.tasks ?? []}
            taskFilter={taskFilter}
            onTaskFilterChange={setTaskFilter}
            filteredEpisodeCount={visibleEpisodes.length}
            annotationSummary={annotationSummary ?? undefined}
            episodeOutcomes={mergedOutcomes ?? undefined}
            humanOutcomes={humanOutcomeKeys}
            recapFractions={recapFractions ?? undefined}
            onOutcomeChange={
              isAnnotateBackendEnabled() ? changeOutcome : undefined
            }
            onEpisodeSelect={
              activeTab === "urdf"
                ? (ep) => {
                    setUrdfEpisode(ep);
                    urdfChangerRef.current?.(ep);
                  }
                : activeTab === "annotations"
                  ? (ep) => router.push(`./episode_${ep}`)
                  : undefined
            }
          />
        )}

        {/* Main content */}
        <main
          className="vw-main vw-chart"
          // Focusable so the content scrolls by keyboard (axe
          // scrollable-region-focusable) when it holds no control.
          tabIndex={0}
          data-loading={isLoading ? "true" : undefined}
          aria-busy={isLoading || undefined}
        >
          {isLoading && <Loading />}

          {activeTab === "episodes" && (
            <>
              {heading}

              <RawCaptureNotice compact />

              {/* Videos */}
              {videosInfo.length > 0 && (
                <SimpleVideosPlayer
                  videosInfo={videosInfo}
                  onVideosReady={() => setVideosReady(true)}
                  annotationEpisodeId={episodeId}
                  annotationRepoId={datasetInfo.repoId}
                />
              )}

              {/* Language instruction */}
              {task && (
                <section className="vw-card">
                  <h2 className="vw-label">
                    <T>Language Instruction</T>
                  </h2>
                  <div className="mt-1.5 space-y-0.5">
                    {task
                      .split("\n")
                      .map((instruction: string, index: number) => (
                        <p key={index} className="m-0">
                          {t(instruction)}
                        </p>
                      ))}
                  </div>
                </section>
              )}

              {/* Graph */}
              <Suspense fallback={null}>
                <DataRecharts
                  data={chartDataGroups}
                  onChartsReady={() => setChartsReady(true)}
                />
              </Suspense>

              <PlaybackBar />
            </>
          )}

          {activeTab === "annotations" && (
            <div className="annotations-skin flex flex-col gap-4">
              {heading}
              {videosInfo.length > 0 && (
                <SimpleVideosPlayer
                  videosInfo={videosInfo}
                  onVideosReady={() => setVideosReady(true)}
                  annotationEpisodeId={episodeId}
                  annotationRepoId={datasetInfo.repoId}
                />
              )}
              <PlaybackBar />

              {/* Sub-tabs: language/event annotation vs. SAM3 object
              annotation are fully independent systems — keep the video
              player + scrubber shared above (both need it, and keeping
              it mounted across sub-tab switches avoids a reload), but
              split everything else so users always know which system
              they're working in. */}
              <Tabs
                label={t("Annotation type")}
                value={annotationsSubTab}
                onChange={(id) =>
                  setAnnotationsSubTab(id as "language" | "vision")
                }
                items={[
                  {
                    id: "language",
                    label: t("Language & Events"),
                    icon: MessageSquareText,
                  },
                  {
                    id: "vision",
                    label: t("Objects & Tracking"),
                    icon: ScanSearch,
                  },
                ]}
              />

              {annotationsSubTab === "language" && (
                <>
                  <div className="grounding-intro">
                    <h2 className="vw-label">
                      <T>Grounded VQA</T>
                    </h2>
                    <ul>
                      <li>
                        <T>
                          Draw directly on the active video to create visual
                          questions. Drag for a bounding box, click for a point.
                          The camera is detected from the video you draw on.
                        </T>
                      </li>
                      <li>
                        <T>
                          Drag on any video to add a bbox question. Click any
                          video to add a keypoint question. Confirm the popup
                          with{" "}
                        </T>
                        <Kbd>↵</Kbd>
                        <T> or </T>
                        <Kbd>Ctrl/Cmd+S</Kbd>
                        <T>, or cancel with </T>
                        <Kbd>{t("Esc")}</Kbd>.
                      </li>
                    </ul>
                  </div>
                  <AnnotationsTimeline duration={data.duration} />
                  <AnnotationsPanel
                    cameraKeys={videosInfo.map((v) => v.filename)}
                  />
                </>
              )}

              {annotationsSubTab === "vision" && (
                <>
                  <FastSegmentationPanel
                    episodeId={episodeId}
                    ident={{ repoId: datasetInfo.repoId }}
                    cameraKeys={videosInfo.map((v) => v.filename)}
                    allEpisodes={availableEpisodes}
                  />
                  <ObjectAnnotationPanel
                    episodeId={episodeId}
                    ident={{ repoId: datasetInfo.repoId }}
                    cameraKeys={videosInfo.map((v) => v.filename)}
                    allEpisodes={availableEpisodes}
                    taskIndex={taskIndex}
                  />
                </>
              )}
            </div>
          )}

          {activeTab === "statistics" && (
            <StatsPanel
              datasetInfo={datasetInfo}
              taskCount={taskIndex?.tasks.length}
              episodeLengthStats={episodeLengthStats}
              loading={statsLoading}
            />
          )}

          {activeTab === "frames" && (
            <OverviewPanel
              data={episodeFramesData}
              loading={framesLoading}
              flaggedOnly={framesFlaggedOnly}
              onFlaggedOnlyChange={setFramesFlaggedOnly}
            />
          )}

          {activeTab === "analysis" && (
            <AnalysisTab
              view={analysisView}
              onViewChange={handleAnalysisViewChange}
            >
              {(view) =>
                view === "insights" ? (
                  <Suspense fallback={<Loading />}>
                    <ActionInsightsPanel
                      flatChartData={data.flatChartData}
                      fps={datasetInfo.fps}
                      crossEpisodeData={crossEpData}
                      crossEpisodeLoading={insightsLoading}
                      totalEpisodes={datasetInfo.total_episodes}
                      tasks={taskIndex?.tasks ?? []}
                      crossEpisodeRequest={insightsRequest}
                      onCrossEpisodeRequestChange={applyInsightsRequest}
                      crossEpisodeProgress={insightsProgress}
                    />
                  </Suspense>
                ) : view === "filtering" ? (
                  <Suspense fallback={<Loading />}>
                    <FilteringPanel
                      repoId={datasetInfo.repoId}
                      crossEpisodeData={crossEpData}
                      crossEpisodeLoading={insightsLoading}
                      episodeLengthStats={episodeLengthStats}
                      flatChartData={data.flatChartData}
                      onViewFlaggedEpisodes={() => {
                        setSidebarFlaggedOnly(true);
                        handleTabChange("episodes");
                      }}
                    />
                  </Suspense>
                ) : (
                  <LeviDoctor repoId={`${org}/${dataset}`} />
                )
              }
            </AnalysisTab>
          )}

          {activeTab === "urdf" && (
            <Suspense fallback={<Loading />}>
              <URDFViewer
                data={data}
                org={org}
                dataset={dataset}
                episodeChangerRef={urdfChangerRef}
                playToggleRef={urdfPlayToggleRef}
              />
            </Suspense>
          )}
        </main>
      </div>
    </div>
  );
}
