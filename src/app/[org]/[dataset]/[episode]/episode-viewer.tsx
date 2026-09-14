// Modified for LEVI (2026); see NOTICE and docs/UPSTREAM.md.
"use client";
import { T } from "@/components/levi-locale";

import { useState, useEffect, useMemo, useRef, lazy, Suspense } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { postParentMessageWithParams } from "@/utils/postParentMessage";
import { SimpleVideosPlayer } from "@/components/simple-videos-player";
import PlaybackBar from "@/components/playback-bar";
import { TimeProvider, useTime } from "@/context/time-context";
import { FlaggedEpisodesProvider } from "@/context/flagged-episodes-context";
import {
  AnnotationsProvider,
  useAnnotations,
} from "@/context/annotations-context";
import { AnnotationsPanel } from "@/components/annotations-panel";
import ObjectAnnotationPanel from "@/components/object-annotation-panel";
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
import {
  fetchAnnotationSummary,
  isAnnotateBackendEnabled,
  type AnnotationSummary,
} from "@/utils/annotationsClient";

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

type ActiveTab =
  | "episodes"
  | "annotations"
  | "statistics"
  | "frames"
  | "insights"
  | "filtering"
  | "doctor"
  | "urdf";

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

// Hoisted to module scope. Defining inside EpisodeViewerInner created a new
// component type on every parent render — and the parent re-renders ~12.5×/s
// during playback because it consumes `currentTime` from useTime. React
// would unmount and remount every tab on every tick.
function TabButton({
  active,
  onClick,
  label,
  title,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
  title?: string;
}) {
  return (
    <T>
      {
        <button
          onClick={onClick}
          title={title}
          className={`relative px-5 py-3 text-xs font-medium tracking-wide uppercase transition-colors ${
            active ? "text-cyan-300" : "text-slate-400 hover:text-slate-100"
          }`}
        >
          <T>{label}</T>
          <span
            className={`pointer-events-none absolute bottom-0 left-3 right-3 h-px transition-all ${
              active
                ? "bg-cyan-400 shadow-[0_0_8px_rgba(56,189,248,0.55)]"
                : "bg-transparent"
            }`}
          />
        </button>
      }
    </T>
  );
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
      <T>
        {
          <div className="flex h-screen items-center justify-center bg-[var(--bg)] text-red-300">
            <div className="panel-raised max-w-xl p-6 border-red-500/40">
              <h2 className="text-xl font-medium mb-3">
                <T>Something went wrong</T>
              </h2>
              <p className="text-sm font-mono whitespace-pre-wrap text-red-200/90">
                <T>{error}</T>
              </p>
            </div>
          </div>
        }
      </T>
    );
  }

  if (!data) {
    return (
      <T>
        {
          <div className="relative h-screen bg-[var(--bg)]">
            <Loading />
          </div>
        }
      </T>
    );
  }

  return (
    <T>
      {
        <TimeProvider duration={data!.duration}>
          <FlaggedEpisodesProvider
            key={`${org}/${dataset}`}
            repoId={`${org}/${dataset}`}
          >
            <AnnotationsProvider>
              <EpisodeBootstrap data={data!} />
              <EpisodeViewerInner data={data!} org={org} dataset={dataset} />
            </AnnotationsProvider>
          </FlaggedEpisodesProvider>
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

  const [videosReady, setVideosReady] = useState(!videosInfo.length);
  const [chartsReady, setChartsReady] = useState(false);

  const loadStartRef = useRef(performance.now());

  const router = useRouter();
  const searchParams = useSearchParams();

  // Tab state & lazy stats — read sessionStorage in the initializer so the
  // correct tab renders on the very first frame (no post-mount flash).
  // Safe because EpisodeViewerInner only mounts client-side (behind a loading gate).
  const [activeTab, setActiveTab] = useState<ActiveTab>(() => {
    if (typeof window !== "undefined") {
      const stored = readBrowserStorage("session", "activeTab");
      if (
        stored &&
        [
          "episodes",
          "annotations",
          "statistics",
          "frames",
          "insights",
          "filtering",
          "urdf",
          "doctor",
        ].includes(stored)
      ) {
        return stored as ActiveTab;
      }
    }
    return "episodes";
  });
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

  // Re-trigger data loading for the restored tab on mount
  useEffect(() => {
    if (activeTab === "statistics") loadStats();
    if (activeTab === "frames") loadFrames();
    if (activeTab === "insights") loadInsights();
    if (activeTab === "filtering") {
      loadStats();
      loadInsights();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleTabChange = (tab: ActiveTab) => {
    setActiveTab(tab);
    if (tab === "statistics") loadStats();
    if (tab === "frames") loadFrames();
    if (tab === "insights") loadInsights();
    if (tab === "filtering") {
      loadStats();
      loadInsights();
    }
  };

  // `currentTime` is intentionally NOT read here. Subscribing to it would
  // re-render this 700-line component every ~80ms during playback. The
  // <UrlTimeSync /> child handles its only consumer (the ?t= URL writer).
  // `seek` and `setIsPlaying` are stable references from useCallback /
  // useState — they don't drive renders.
  const { seek, setIsPlaying } = useTime();

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
    const timeParam = searchParams.get("t");
    if (timeParam) {
      const timeValue = parseFloat(timeParam);
      if (!isNaN(timeValue)) {
        seek(timeValue);
      }
    }
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
        if (s.taskFilter && s.visibleEpisodes.length === 0) return;
        const navigationEpisodes = s.taskFilter
          ? s.visibleEpisodes
          : s.visibleEpisodes.length > 0
            ? s.visibleEpisodes
            : s.episodes;
        const current = s.activeTab === "urdf" ? s.urdfEpisode : s.episodeId;
        const currentIndex = navigationEpisodes.indexOf(current);
        const delta = key === "ArrowDown" ? 1 : -1;
        const nextIndex =
          currentIndex === -1
            ? delta > 0
              ? 0
              : navigationEpisodes.length - 1
            : currentIndex + delta;
        const nextEp = navigationEpisodes[nextIndex];
        if (nextEp === undefined) return;
        if (s.activeTab === "urdf") {
          setUrdfEpisode(nextEp);
          urdfChangerRef.current?.(nextEp);
        } else {
          router.push(`./episode_${nextEp}`);
        }
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

  const renderTab = (tab: ActiveTab, label: string, title?: string) => (
    <TabButton
      active={activeTab === tab}
      onClick={() => handleTabChange(tab)}
      label={label}
      title={title}
    />
  );

  return (
    <T>
      {
        <div className="flex flex-col h-screen max-h-screen bg-[var(--bg)] text-[var(--text-primary)]">
          <UrlTimeSync />
          {/* Top tab bar */}
          <div className="flex items-center border-b border-white/5 bg-[var(--surface-0)] shrink-0 overflow-x-auto">
            {renderTab("episodes", "Episodes")}
            {renderTab(
              "annotations",
              "Annotations",
              "Edit subtask / plan / memory / interjection / VQA atoms (lerobot v3.1 schema)",
            )}
            {hasURDFSupport(datasetInfo.robot_type) &&
              renderTab("urdf", "3D Replay")}
            {renderTab("statistics", "Statistics")}
            {renderTab("filtering", "Filtering")}
            {renderTab("frames", "Frame gallery")}
            {renderTab("insights", "Action Insights")}
            {renderTab(
              "doctor",
              "Doctor",
              "Dataset quality diagnostics (powered by lerobot-doctor)",
            )}
            <div className="ml-auto flex items-center">
              <LeviReview repoId={`${org}/${dataset}`} />
              <HfAuthButton variant="tab" />
            </div>
          </div>

          {/* Body: sidebar + content */}
          <div className="flex flex-1 min-h-0">
            {/* Sidebar — on Episodes and 3D Replay tabs */}
            {(activeTab === "episodes" ||
              activeTab === "annotations" ||
              activeTab === "urdf") && (
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
                episodeOutcomes={episodeOutcomes ?? undefined}
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
            <div
              className={`flex flex-col gap-4 p-4 flex-1 relative ${isLoading ? "overflow-hidden" : "overflow-y-auto"}`}
            >
              {isLoading && <Loading />}

              {activeTab === "episodes" && (
                <>
                  <div className="flex items-center gap-4 mb-2">
                    <a
                      href="https://github.com/huggingface/lerobot"
                      target="_blank"
                      className="block shrink-0 opacity-90 hover:opacity-100 transition-opacity"
                    >
                      {/* eslint-disable-next-line @next/next/no-img-element */}
                      <img
                        src="https://github.com/huggingface/lerobot/raw/main/media/readme/lerobot-logo-thumbnail.png"
                        alt="LeRobot Logo"
                        className="w-24"
                      />
                    </a>

                    <div className="min-w-0">
                      <a
                        href={`https://huggingface.co/datasets/${datasetInfo.repoId}`}
                        target="_blank"
                        className="text-slate-200 hover:text-cyan-300 transition-colors"
                      >
                        <p className="text-base font-medium truncate">
                          <T>{datasetInfo.repoId}</T>
                        </p>
                      </a>
                      <p className="text-[10px] uppercase tracking-wide text-slate-500 mt-0.5 tabular">
                        <T>Episode · </T>
                        <T>{episodeId}</T>
                      </p>
                    </div>
                  </div>

                  {/* Videos */}
                  {videosInfo.length > 0 && (
                    <SimpleVideosPlayer
                      videosInfo={videosInfo}
                      onVideosReady={() => setVideosReady(true)}
                      annotationEpisodeId={episodeId}
                      annotationRepoId={datasetInfo.repoId}
                    />
                  )}

                  {/* Language Instruction */}
                  {task && (
                    <div className="mb-6 panel p-4">
                      <p className="text-[10px] uppercase tracking-wide text-slate-500">
                        <T>Language Instruction</T>
                      </p>
                      <div className="mt-1.5 space-y-0.5 text-sm text-slate-200">
                        {task
                          .split("\n")
                          .map((instruction: string, index: number) => (
                            <p key={index}>
                              <T>{instruction}</T>
                            </p>
                          ))}
                      </div>
                    </div>
                  )}

                  {/* Graph */}
                  <div className="mb-4">
                    <Suspense fallback={null}>
                      <DataRecharts
                        data={chartDataGroups}
                        onChartsReady={() => setChartsReady(true)}
                      />
                    </Suspense>
                  </div>

                  <PlaybackBar />
                </>
              )}

              {activeTab === "annotations" && (
                <div className="annotations-skin flex flex-col gap-4">
                  <div className="flex items-center gap-3">
                    <p className="text-base font-medium text-slate-200 truncate">
                      <T>{datasetInfo.repoId}</T>
                    </p>
                    <p className="text-[10px] uppercase tracking-wide text-slate-500 tabular">
                      <T>Episode · </T>
                      <T>{episodeId}</T>
                    </p>
                  </div>
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
                  <div className="flex items-center border-b border-white/5 -mx-1">
                    <TabButton
                      active={annotationsSubTab === "language"}
                      onClick={() => setAnnotationsSubTab("language")}
                      label="Language & Events"
                      title="Task augmentation, subtask, plan, memory, interjection, VQA"
                    />
                    <TabButton
                      active={annotationsSubTab === "vision"}
                      onClick={() => setAnnotationsSubTab("vision")}
                      label="Objects & Tracking"
                      title="SAM3 object detection, tracking and mask review"
                    />
                  </div>

                  {annotationsSubTab === "language" && (
                    <>
                      <div className="grounding-intro">
                        <span className="section-kicker">
                          <T>Grounded VQA</T>
                        </span>
                        <ul>
                          <li>
                            <T>
                              Draw directly on the active video to create visual
                              questions. Drag for a bounding box, click for a
                              point. The camera is detected from the video you
                              draw on.
                            </T>
                          </li>
                          <li>
                            <T>
                              Drag on any video to add a bbox question. Click
                              any video to add a keypoint question. Confirm the
                              popup with{" "}
                            </T>
                            <kbd>↵</kbd>
                            <T> or </T>
                            <kbd>Ctrl/Cmd+S</kbd>
                            <T>, or cancel with </T>
                            <kbd>
                              <T>Esc</T>
                            </kbd>
                            .
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
                    <ObjectAnnotationPanel
                      episodeId={episodeId}
                      ident={{ repoId: datasetInfo.repoId }}
                      cameraKeys={videosInfo.map((v) => v.filename)}
                      allEpisodes={availableEpisodes}
                      taskIndex={taskIndex}
                    />
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

              {activeTab === "insights" && (
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
              )}

              {activeTab === "filtering" && (
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
              )}

              {activeTab === "doctor" && (
                <LeviDoctor repoId={`${org}/${dataset}`} />
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
            </div>
          </div>
        </div>
      }
    </T>
  );
}
