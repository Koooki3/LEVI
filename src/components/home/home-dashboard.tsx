"use client";
/**
 * The home page as a work entrance (design proposal §8.2): continue where
 * you were, what needs you, what is running, the live evaluation and recent
 * datasets. Read-only, from existing API answers (home-data.ts) and this
 * browser's own visit list (shell/recent.ts). Styles: src/styles/home.css.
 */
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";
import {
  ArrowRight,
  BookOpen,
  Bot,
  Database,
  FolderOpen,
  History,
  Inbox,
  Activity,
  Compass,
  Search,
} from "lucide-react";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  Icon,
  Progress,
  Skeleton,
} from "@/components/ds";
import { leviApi } from "@/components/levi-api";
import { useLocale } from "@/components/levi-locale";
import { offersTrainingPool } from "@/components/live/embedding";
import { PulseDot, usePulseNote } from "@/components/live/live-nav";
import { useLivePulse } from "@/components/live/use-live-pulse";
import { LeviMark } from "@/components/shell/brand";
import {
  readRecent,
  visitHref,
  type RecentVisit,
} from "@/components/shell/recent";
import { toggleAgentWorkbench } from "@/components/shell/shell-events";
import {
  STEP_LABEL,
  catalogDatasets,
  greeting,
  pendingTasks,
  recentDatasets,
  relativeTime,
  runningJobs,
  type DatasetSummary,
  type PendingTask,
  type RunningJob,
} from "./home-data";

const POLL_MS = 15_000;
const DATASET_ID = /^[\w.-]+\/[\w.-]+$/;

type State = {
  /** Each card shows its skeleton until its own answer arrives. */
  datasets: DatasetSummary[] | undefined;
  jobs: RunningJob[] | undefined;
  /** null: the agent API did not answer (no access or not running). */
  pending: PendingTask[] | null | undefined;
};

/** A request that gives up after `ms` (a slow answer must not keep a card
 * in its loading state). */
function within<T>(ms: number, work: Promise<T>): Promise<T | null> {
  return Promise.race([
    work.catch(() => null),
    new Promise<null>((resolve) => setTimeout(() => resolve(null), ms)),
  ]);
}

function useHomeData(pool: boolean): State {
  const [state, setState] = useState<State>({
    datasets: undefined,
    jobs: undefined,
    pending: undefined,
  });
  const busy = useRef(false);
  const load = useCallback(async () => {
    if (busy.current) return;
    busy.current = true;
    const patch = (part: Partial<State>) =>
      setState((previous) => ({ ...previous, ...part }));
    try {
      await Promise.all([
        within(10_000, leviApi<unknown>("catalog")).then((catalog) =>
          patch({ datasets: catalogDatasets(catalog) }),
        ),
        Promise.all([
          within(10_000, leviApi<unknown>("jobs")),
          pool ? within(10_000, leviApi<unknown>("pool/jobs")) : null,
        ]).then(([conversions, poolJobs]) =>
          patch({ jobs: runningJobs(poolJobs, conversions) }),
        ),
        within(
          10_000,
          fetch("/api/levi/agent/v1/activity/tasks?limit=50", {
            cache: "no-store",
          }).then((response) => (response.ok ? response.json() : null)),
        ).then((tasks) =>
          patch({ pending: tasks === null ? null : pendingTasks(tasks) }),
        ),
      ]);
    } finally {
      busy.current = false;
    }
  }, [pool]);
  useEffect(() => {
    void load();
    const timer = setInterval(() => {
      if (document.visibilityState === "visible") void load();
    }, POLL_MS);
    const onVisible = () => {
      if (document.visibilityState === "visible") void load();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [load]);
  return state;
}

function useNow(): number | null {
  // Rendered after mount: the server and the first client render agree.
  const [now, setNow] = useState<number | null>(null);
  useEffect(() => {
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), 60_000);
    return () => clearInterval(timer);
  }, []);
  return now;
}

function Ago({ at, now }: { at: number; now: number | null }) {
  const { t } = useLocale();
  if (now === null) return null;
  const { key, n } = relativeTime(at, now);
  return <>{t(key).replace("{n}", String(n))}</>;
}

function OpenDataset() {
  const { t } = useLocale();
  const router = useRouter();
  const [query, setQuery] = useState("");
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const invalid = query.trim() !== "" && !DATASET_ID.test(query.trim());
  useEffect(() => {
    const controller = new AbortController();
    if (!query.trim()) {
      setSuggestions([]);
      return;
    }
    const timer = setTimeout(
      () =>
        fetch(
          `https://huggingface.co/api/datasets?search=${encodeURIComponent(query)}&limit=5`,
          { signal: controller.signal },
        )
          .then((r) => r.json())
          .then((data) =>
            setSuggestions(
              Array.isArray(data) ? data.map((d: { id: string }) => d.id) : [],
            ),
          )
          .catch(() => {}),
      250,
    );
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [query]);
  function open(id: string) {
    if (DATASET_ID.test(id.trim())) router.push(`/${id.trim()}`);
  }
  return (
    <div className="levi-home-open">
      <form
        className="levi-home-search"
        role="search"
        onSubmit={(event: FormEvent) => {
          event.preventDefault();
          open(query);
        }}
      >
        <Icon icon={Search} className="levi-home-search__icon" />
        <input
          className="levi-home-search__input ds-focus"
          aria-label={t("Dataset ID")}
          aria-invalid={invalid || undefined}
          placeholder={t("Search or enter a Hugging Face dataset ID")}
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          required
          pattern="[\w.\-]+/[\w.\-]+"
        />
        <Button type="submit" variant="primary" iconEnd={ArrowRight}>
          {t("Open dataset")}
        </Button>
      </form>
      {suggestions.length > 0 && (
        <ul className="levi-home-suggestions" aria-label={t("Suggestions")}>
          {suggestions.map((id) => (
            <li key={id}>
              <button
                type="button"
                className="ds-focus"
                onClick={() => open(id)}
              >
                <span className="levi-home-mono">{id}</span>
                <Icon icon={ArrowRight} />
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function LiveCard() {
  const { t } = useLocale();
  const { enabled, pulse } = useLivePulse();
  const note = usePulseNote(pulse);
  if (!enabled) return null;
  return (
    <Link href="/live" className="levi-home-live ds-focus">
      <PulseDot pulse={pulse} />
      <span className="levi-home-live__text">
        <strong>{t("Live evaluation & annotation")}</strong>
        <span>{note}</span>
      </span>
      {pulse.count > 0 && (
        <Badge tone={pulse.light === "red" ? "danger" : "warning"}>
          {String(Math.min(pulse.count, 99))}
        </Badge>
      )}
      <span className="levi-home-live__go">
        <span>{t("Open the live page")}</span>
        <Icon icon={ArrowRight} />
      </span>
    </Link>
  );
}

function ContinueCard({
  visit,
  name,
  now,
}: {
  visit: RecentVisit;
  name: string;
  now: number | null;
}) {
  const { t } = useLocale();
  return (
    <Link href={visitHref(visit)} className="levi-home-continue ds-focus">
      <span className="levi-home-continue__icon" aria-hidden="true">
        <Icon icon={History} size="md" />
      </span>
      <span className="levi-home-continue__text">
        <span className="levi-home-label">{t("Continue")}</span>
        <strong>
          <span className="levi-home-mono">{name}</span>
          {visit.episode !== null && (
            <>
              {" · "}
              {t("Episode {n}").replace("{n}", String(visit.episode))}
            </>
          )}
        </strong>
        <span className="levi-home-meta">
          <Ago at={visit.at} now={now} />
        </span>
      </span>
      <span className="levi-home-continue__go">
        {t("Continue")}
        <Icon icon={ArrowRight} />
      </span>
    </Link>
  );
}

function ListSkeleton() {
  return (
    <div className="levi-home-skeleton" aria-hidden="true">
      <Skeleton height={16} width="70%" />
      <Skeleton height={12} width="45%" />
      <Skeleton height={16} width="60%" />
    </div>
  );
}

function PendingCard({
  pending,
  loaded,
}: {
  pending: PendingTask[] | null;
  loaded: boolean;
}) {
  const { t } = useLocale();
  const count = pending?.length ?? 0;
  return (
    <Card
      className="levi-home-card"
      aria-busy={!loaded || undefined}
      title={
        <>
          {t("Needs you")}
          {count > 0 && <span className="levi-home-count">{count}</span>}
        </>
      }
      description={t("Agent tasks waiting for a person's decision.")}
    >
      {!loaded ? (
        <ListSkeleton />
      ) : pending === null ? (
        <p className="levi-home-note">
          {t("Agent tasks could not be read just now.")}
        </p>
      ) : count === 0 ? (
        <EmptyState
          className="levi-home-empty"
          icon={Inbox}
          title={t("Nothing waits for you.")}
        />
      ) : (
        <>
          <ul className="levi-home-list">
            {pending!.slice(0, 5).map((task) => (
              <li key={task.runId}>
                <span className="levi-home-list__main">
                  <strong>{t(STEP_LABEL[task.step])}</strong>
                  <span className="levi-home-meta">
                    <span className="levi-home-mono">{task.dataset}</span>
                    {task.episodes > 0 &&
                      ` · ${t("{n} episodes").replace("{n}", String(task.episodes))}`}
                  </span>
                </span>
              </li>
            ))}
          </ul>
          <Button
            variant="secondary"
            size="sm"
            icon={Bot}
            onClick={toggleAgentWorkbench}
          >
            {t("Open the Agent Workbench")}
          </Button>
        </>
      )}
    </Card>
  );
}

function RunningCard({
  jobs,
  loaded,
}: {
  jobs: RunningJob[];
  loaded: boolean;
}) {
  const { t } = useLocale();
  return (
    <Card
      className="levi-home-card"
      aria-busy={!loaded || undefined}
      title={
        <>
          {t("Running")}
          {jobs.length > 0 && (
            <span className="levi-home-count">{jobs.length}</span>
          )}
        </>
      }
      description={t("Conversions and training pool jobs.")}
    >
      {!loaded ? (
        <ListSkeleton />
      ) : jobs.length === 0 ? (
        <EmptyState
          className="levi-home-empty"
          icon={Activity}
          title={t("No job is running.")}
        />
      ) : (
        <ul className="levi-home-list">
          {jobs.slice(0, 5).map((job) => (
            <li key={`${job.kind}-${job.id}`}>
              <Link href={job.href} className="levi-home-job ds-focus">
                <span className="levi-home-list__main">
                  <strong>{t(job.title)}</strong>
                  <span className="levi-home-meta">
                    {job.subject && (
                      <span className="levi-home-mono">{job.subject}</span>
                    )}
                    {job.subject && " · "}
                    {t(job.stage)}
                  </span>
                </span>
                <Progress
                  className="levi-home-job__bar"
                  label={`${t(job.title)} ${job.subject}`}
                  value={job.fraction === null ? null : job.fraction * 100}
                />
              </Link>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function DatasetsCard({
  datasets,
  loaded,
  now,
}: {
  datasets: ReturnType<typeof recentDatasets>;
  loaded: boolean;
  now: number | null;
}) {
  const { t } = useLocale();
  return (
    <Card
      className="levi-home-card"
      aria-busy={!loaded || undefined}
      title={t("Recent datasets")}
      description={t("Opened in this browser, then the other local ones.")}
      actions={
        <Link href="/explore" className="levi-home-link ds-focus">
          {t("Explore")}
        </Link>
      }
    >
      {!loaded ? (
        <ListSkeleton />
      ) : datasets.length === 0 ? (
        <EmptyState
          className="levi-home-empty"
          icon={Database}
          title={t("No local dataset yet.")}
          action={
            <Link href="/workbench" className="levi-home-cta ds-focus">
              <Icon icon={FolderOpen} />
              {t("Register a local dataset")}
            </Link>
          }
        />
      ) : (
        <ul className="levi-home-list">
          {datasets.map((item) => (
            <li key={item.repo}>
              <Link
                href={visitHref({ repo: item.repo, episode: null })}
                className="levi-home-dataset ds-focus"
              >
                <Icon icon={Database} />
                <span className="levi-home-list__main">
                  <strong className="levi-home-mono">{item.name}</strong>
                  <span className="levi-home-meta">
                    {item.episodes !== null &&
                      t("{n} episodes").replace("{n}", String(item.episodes))}
                    {item.episodes !== null && item.visitedAt !== null && " · "}
                    {item.visitedAt !== null && (
                      <Ago at={item.visitedAt} now={now} />
                    )}
                    {item.kind === "raw" && ` · ${t("Raw capture")}`}
                  </span>
                </span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

export function HomeDashboard() {
  const { t } = useLocale();
  const { enabled, embedded } = useLivePulse();
  const pool = offersTrainingPool(enabled, embedded);
  const { datasets, jobs, pending } = useHomeData(pool);
  const now = useNow();
  const [recent, setRecent] = useState<RecentVisit[]>([]);
  useEffect(() => setRecent(readRecent()), []);
  const last = recent.find((visit) => visit.episode !== null) ?? recent[0];
  const names = new Map((datasets ?? []).map((item) => [item.repo, item.name]));
  const hour = now === null ? null : new Date(now).getHours();

  return (
    <main className="levi-home">
      <header className="levi-home-head">
        <div className="levi-home-title">
          <LeviMark size={40} />
          <div>
            <h1>{hour === null ? "LEVI" : t(greeting(hour))}</h1>
            <p>
              {t("Where you left off, what needs you and what is running.")}
            </p>
          </div>
        </div>
        <OpenDataset />
      </header>

      <LiveCard />

      {last && (
        <ContinueCard
          visit={last}
          name={names.get(last.repo) ?? last.repo}
          now={now}
        />
      )}

      <h2 className="ds-sr-only">{t("Your work")}</h2>
      <div className="levi-home-grid">
        <PendingCard pending={pending ?? null} loaded={pending !== undefined} />
        <RunningCard jobs={jobs ?? []} loaded={jobs !== undefined} />
        <DatasetsCard
          datasets={recentDatasets(recent, datasets ?? [])}
          loaded={datasets !== undefined}
          now={now}
        />
      </div>

      <nav className="levi-home-more" aria-label={t("More")}>
        <Link href="/guide" className="levi-home-link ds-focus">
          <Icon icon={BookOpen} />
          {t("About LEVI and the guide")}
        </Link>
        <Link href="/workbench" className="levi-home-link ds-focus">
          <Icon icon={FolderOpen} />
          {t("Open a local dataset")}
        </Link>
        <Link href="/explore" className="levi-home-link ds-focus">
          <Icon icon={Compass} />
          {t("Explore all datasets")}
        </Link>
      </nav>
    </main>
  );
}
