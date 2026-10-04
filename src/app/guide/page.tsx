"use client";
/**
 * Guide: what LEVI is (the introduction that used to be the home page) and
 * how to use it, in the reading layout (src/styles/reading.css): contents on
 * the left that mark the current section, one column of text.
 */
import Link from "next/link";
import { useEffect, useState } from "react";
import { ArrowRight, Eye, Microscope, Sparkles } from "lucide-react";
import { Icon } from "@/components/ds";
import { T, useLocale } from "@/components/levi-locale";
import { LeviMark } from "@/components/shell/brand";
import "@/styles/reading.css";
import "@/styles/home.css";

// Public LeRobot datasets, checked reachable on 2026-09-20; keep in step with
// DEMOS in levi/catalog.py.
const DEMOS = [
  { id: "lerobot/svla_so101_pickplace", title: "Demonstration collection" },
  { id: "lerobot/aloha_static_coffee", title: "Policy evaluation" },
];

const STEPS = [
  {
    icon: Eye,
    title: "Observe",
    body: "Synchronized cameras, signals and 3D robot replay.",
  },
  {
    icon: Microscope,
    title: "Understand",
    body: "Temporal alignment, action quality and grounded annotations.",
  },
  {
    icon: Sparkles,
    title: "Refine",
    body: "Convert with the built-in pipeline. Review, diagnose and export.",
  },
];

const SECTIONS: [string, string][] = [
  [
    "01 / Observe",
    "Open a Hub dataset, or register a local LeRobot dataset or a raw capture folder (task/demo_NNNN) — a raw capture is shown through a lossless browsing view with every frame. Episodes synchronize camera playback and signal charts. Space toggles playback; arrow keys navigate episodes. Clicking a segment on the timeline seeks to its exact time. Use Frames to compare first and last frames.",
  ],
  [
    "02 / Annotate",
    "Select Annotations to edit task augmentation, subtask, plan, memory, speech and VQA. Drag on a video for a bounding box or click for a keypoint. Click the dot beside an episode to label it success or failure. Save episode persists edits; Save dataset creates a new annotated export. Annotations made on a raw capture carry over when it is converted.",
  ],
  [
    "03 / Segment",
    "Under Annotations → Objects, Fast segmentation outlines and tracks objects while an episode plays (Live overlay) and labels whole datasets offline, using a small student model distilled from SAM3. Choose Quality (teacher model) to label with SAM3 itself. The student runs in its own worker environment (docs/SEGMENTATION.md). Results are ordinary object annotations, suggested until a person reviews them.",
  ],
  [
    "04 / Diagnose",
    "Statistics describes metadata and episode lengths. Filtering ranks low movement, sudden motion and unusual lengths. Action Insights computes autocorrelation, temporal alignment, speed variance and cross-episode variance. Doctor runs version-aware checks.",
  ],
  [
    "05 / Review",
    "Flag suspicious episodes and export a review manifest. An agent or a local model can propose subtask segments and outcomes in the Agent Workbench; a person reviews, commits and can undo. A release-anchored review judges success at every recorded robot event, such as each gripper opening, with one narrow question per event, and shows its verdicts on the annotations timeline (docs/ANCHORED_REVIEW.md).",
  ],
  [
    "06 / Convert",
    "In the conversion workbench, inspect an input to see which requirements it meets and which exports it supports — LeRobot v2.1 or a RECAP value dataset — then choose options, review the plan and follow the run's live progress.",
  ],
  [
    "07 / Train",
    "For local datasets, the Value model section of the annotations view computes per-frame values and advantage labels from a RECAP value checkpoint and shows them on the timeline, with each episode's share of positive-advantage frames. Training manifests (levi export manifest, or the API) state which frames enter a learner's loss and with what weight, with the evidence behind each choice; the dataset itself is never rewritten (docs/TRAINING_MANIFEST.md).",
  ],
  [
    "08 / Pool",
    "The Training pool page indexes every dataset under LEVI_POOL_ROOTS, one row per episode. Scan now builds the index; filter it by category, source, task, outcome and policy. Add tasks to the Composition in the order you want, and give each an episode count, a share of successes and a pick: Smart pick, Random or In order. Dry run previews the counts; Start export writes a merged LeRobot v2.1, RECAP value or raw-capture dataset with a provenance record, and Send to remote copies a finished export to a training machine over SSH. Held-out test episodes are never exported (docs/TRAINING_POOL.md).",
  ],
  [
    "09 / Report",
    "The Report page shows a live technical report from the folder named by LEVI_REPORT_DIR. It follows the language switch and updates when the report files change.",
  ],
  [
    "10 / Share",
    "README.md and README.zh-CN.md cover installation, SSH port forwarding, deployment and troubleshooting. docs/API.md lists the REST routes and agent capabilities; docs/RELEASING.md and CHANGELOG.md cover releases and changes. LEVI preserves the upstream Apache-2.0 license and attribution.",
  ],
];

const sectionId = (index: number) => `guide-${index + 1}`;
const ABOUT = "guide-about";

/** The section at the top of the reading area (for the contents). */
function useCurrentSection(ids: string[]): string {
  const [current, setCurrent] = useState(ids[0]);
  useEffect(() => {
    const onScroll = () => {
      let found = ids[0];
      for (const id of ids) {
        const element = document.getElementById(id);
        if (element && element.getBoundingClientRect().top < 140) found = id;
      }
      setCurrent(found);
    };
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, [ids]);
  return current;
}

const IDS = [ABOUT, ...SECTIONS.map((_, index) => sectionId(index))];

export default function Guide() {
  const { t } = useLocale();
  const current = useCurrentSection(IDS);
  return (
    <main className="levi-reading">
      <div className="levi-reading__head">
        <span className="levi-reading__brand">
          <LeviMark size={18} />
          <span className="levi-eyebrow">
            <T>LEVI / FIELD GUIDE</T>
          </span>
        </span>
      </div>
      <div className="levi-reading__layout">
        <aside className="levi-reading__aside">
          <nav className="levi-toc" aria-label={t("Contents")}>
            <div className="levi-toc__title">{t("Contents")}</div>
            <ol>
              <li>
                <a
                  href={`#${ABOUT}`}
                  aria-current={current === ABOUT ? "location" : undefined}
                >
                  {t("About LEVI")}
                </a>
              </li>
              {SECTIONS.map(([heading], index) => (
                <li key={heading}>
                  <a
                    href={`#${sectionId(index)}`}
                    aria-current={
                      current === sectionId(index) ? "location" : undefined
                    }
                  >
                    {t(heading)}
                  </a>
                </li>
              ))}
            </ol>
          </nav>
        </aside>
        <article className="levi-prose">
          <section id={ABOUT} aria-labelledby={`${ABOUT}-title`}>
            <h1 id={`${ABOUT}-title`}>
              <T>Every motion.</T> <T>A clearer story.</T>
            </h1>
            <p className="levi-prose__lead">
              <T>
                A considered workspace for robot learning data. Observe every
                frame, understand every action, and curate what comes next.
              </T>
            </p>
            <ul className="levi-guide-steps">
              {STEPS.map((step) => (
                <li key={step.title}>
                  <span className="levi-guide-steps__icon" aria-hidden="true">
                    <Icon icon={step.icon} size="md" />
                  </span>
                  <strong>{t(step.title)}</strong>
                  <span>{t(step.body)}</span>
                </li>
              ))}
            </ul>
            <h2 className="levi-guide-sub">
              {t("Start from a public dataset")}
            </h2>
            <ul className="levi-guide-demos">
              {DEMOS.map((demo) => (
                <li key={demo.id}>
                  <Link href={`/${demo.id}`} className="ds-focus">
                    <span className="levi-guide-demos__media">
                      <video
                        src={`/api/proxy/datasets/${demo.id}/resolve/main/videos/chunk-000/observation.images.front/episode_000000.mp4#t=0.1`}
                        muted
                        playsInline
                        preload="metadata"
                        aria-hidden="true"
                        tabIndex={-1}
                        onMouseEnter={(e) =>
                          void e.currentTarget.play().catch(() => {})
                        }
                        onMouseLeave={(e) => e.currentTarget.pause()}
                      />
                    </span>
                    <span className="levi-guide-demos__text">
                      <strong>{t(demo.title)}</strong>
                      <span className="levi-home-mono">{demo.id}</span>
                    </span>
                    <Icon icon={ArrowRight} />
                  </Link>
                </li>
              ))}
            </ul>
            <p className="levi-guide-credit">
              <T>Built on LeRobot Dataset Visualizer · Apache-2.0</T>
            </p>
          </section>
          {SECTIONS.map(([heading, body], index) => (
            <section
              key={heading}
              id={sectionId(index)}
              aria-labelledby={`${sectionId(index)}-title`}
            >
              <h2 id={`${sectionId(index)}-title`}>{t(heading)}</h2>
              <p>{t(body)}</p>
            </section>
          ))}
        </article>
      </div>
    </main>
  );
}
