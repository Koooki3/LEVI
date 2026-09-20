// Modified for LEVI (2026); see NOTICE and docs/UPSTREAM.md.
"use client";
import Link from "next/link";
import { Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import HfAuthButton from "@/components/hf-auth-button";
import { T, useLocale } from "@/components/levi-locale";
// Public LeRobot datasets, checked reachable on 2026-09-20; keep in step with
// DEMOS in levi/catalog.py.
const DEMOS = ["lerobot/svla_so101_pickplace", "lerobot/aloha_static_coffee"];
export default function Home() {
  return (
    <Suspense>
      <Landing />
    </Suspense>
  );
}
function Landing() {
  const [query, setQuery] = useState("");
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const router = useRouter();
  const params = useSearchParams();
  const { t } = useLocale();
  useEffect(() => {
    const path = params.get("path");
    const dataset = params.get("dataset");
    if (path?.startsWith("/") && !path.startsWith("//")) router.replace(path);
    else if (dataset && /^[\w.-]+\/[\w.-]+$/.test(dataset))
      router.replace(
        `/${dataset}${params.get("episode") ? `/episode_${Number(params.get("episode"))}` : ""}${params.get("t") ? `?t=${Number(params.get("t"))}` : ""}`,
      );
  }, [params, router]);
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
    if (/^[\w.-]+\/[\w.-]+$/.test(id.trim())) router.push(`/${id.trim()}`);
  }
  return (
    <main className="levi-home">
      <section className="levi-hero">
        <div>
          <div className="levi-eyebrow">
            <T>EXPLORATION / VALIDATION / INTEGRATION</T>
          </div>
          <h1>
            <T>Every motion.</T>
            <br />
            <em>
              <T>A clearer story.</T>
            </em>
          </h1>
          <p className="levi-intro">
            <T>
              A considered workspace for robot learning data. Observe every
              frame, understand every action, and curate what comes next.
            </T>
          </p>
          <form
            className="levi-search"
            onSubmit={(e) => {
              e.preventDefault();
              open(query);
            }}
          >
            <span>⌕</span>
            <input
              aria-label={t("Dataset ID")}
              placeholder={t("Search or enter a Hugging Face dataset ID")}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              required
              pattern="[\w.\-]+/[\w.\-]+"
            />
            <button>
              <T>Open dataset</T> ↗
            </button>
          </form>
          {suggestions.length > 0 && (
            <div className="levi-suggestions">
              {suggestions.map((id) => (
                <button key={id} onClick={() => open(id)}>
                  <T>{id}</T> ↗
                </button>
              ))}
            </div>
          )}
          <div className="levi-hero-actions">
            <Link href="/workbench">
              <T>Open a local dataset</T> ↗
            </Link>
            <HfAuthButton variant="ghost" />
          </div>
        </div>
        <div className="levi-orbit" aria-hidden="true">
          <div className="levi-orbit-ring" />
          <div className="levi-orbit-ring ring-two" />
          <div className="levi-orbit-core">
            L<span>↗</span>
          </div>
          <span className="levi-orbit-note">
            <T>OBSERVE.</T>
            <br />
            <T>UNDERSTAND.</T>
            <br />
            <T>REFINE.</T>
          </span>
          <span className="levi-orbit-coordinate">
            x 0.032
            <br />y 0.618
            <br />z 0.974
          </span>
        </div>
      </section>
      <section>
        <div className="levi-section-heading">
          <div>
            <span className="levi-eyebrow">
              <T>01 / CURATED STARTING POINTS</T>
            </span>
            <h2>
              <T>Small strawberries. Rich trajectories.</T>
            </h2>
          </div>
          <Link href="/explore">
            <T>Explore all datasets</T> ↗
          </Link>
        </div>
        <div className="levi-demo-grid">
          {DEMOS.map((id, i) => (
            <Link className="levi-demo" href={`/${id}`} key={id}>
              <div className="levi-demo-video">
                <video
                  src={`/api/proxy/datasets/${id}/resolve/main/videos/chunk-000/observation.images.front/episode_000000.mp4#t=0.1`}
                  muted
                  playsInline
                  preload="metadata"
                  onMouseEnter={(e) =>
                    void e.currentTarget.play().catch(() => {})
                  }
                  onMouseLeave={(e) => e.currentTarget.pause()}
                />
                <span className="levi-demo-badge">
                  0{i + 1} / <T>{i === 0 ? "TRAIN" : "EVALUATE"}</T>
                </span>
                <span className="levi-demo-arrow">↗</span>
              </div>
              <div className="levi-demo-caption">
                <div>
                  <h3>
                    <T>
                      {i === 0
                        ? "Demonstration collection"
                        : "Policy evaluation"}
                    </T>
                  </h3>
                  <p>
                    <T>{id}</T>
                  </p>
                </div>
                <span>
                  <T>SO-100</T>
                  <br />
                  <T>3 cameras</T>
                </span>
              </div>
            </Link>
          ))}
        </div>
      </section>
      <section className="levi-workflow">
        <div>
          <span>01</span>
          <h3>
            <T>Observe</T>
          </h3>
          <p>
            <T>Synchronized cameras, signals and 3D robot replay.</T>
          </p>
        </div>
        <div>
          <span>02</span>
          <h3>
            <T>Understand</T>
          </h3>
          <p>
            <T>Temporal alignment, action quality and grounded annotations.</T>
          </p>
        </div>
        <div>
          <span>03</span>
          <h3>
            <T>Refine</T>
          </h3>
          <p>
            <T>
              Convert with the built-in pipeline. Review, diagnose and export.
            </T>
          </p>
        </div>
        <Link href="/workbench">
          <T>Enter the workbench</T> ↗
        </Link>
      </section>
      <footer className="levi-footer">
        <span>
          <T>LEVI / ROBOT DATA ATELIER</T>
        </span>
        <span>
          <T>Built on LeRobot Dataset Visualizer · Apache-2.0</T>
        </span>
      </footer>
    </main>
  );
}
