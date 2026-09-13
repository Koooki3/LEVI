"use client";
import { T } from "@/components/levi-locale";
export default function Guide() {
  return (
    <T>
      {
        <main className="levi-workbench">
          <span className="levi-eyebrow">
            <T>LEVI / FIELD GUIDE</T>
          </span>
          <h1>
            <T>A practical guide to your data.</T>
          </h1>
          {[
            [
              "01 / Observe",
              "Open a Hub dataset or register a local directory. Episodes synchronize camera playback and signal charts. Space toggles playback; arrow keys navigate episodes. Use Frames to compare first and last frames.",
            ],
            [
              "02 / Annotate",
              "Select Annotations to edit task augmentation, subtask, plan, memory, speech and VQA. Drag on a video for a bounding box or click for a keypoint. Save episode persists edits; Save dataset creates a new annotated export.",
            ],
            [
              "03 / Diagnose",
              "Statistics describes metadata and episode lengths. Filtering ranks low movement, sudden motion and unusual lengths. Action Insights computes autocorrelation, temporal alignment, speed variance and cross-episode variance. Doctor runs version-aware checks.",
            ],
            [
              "04 / Refine",
              "Flag suspicious episodes and export a review manifest. In the conversion workbench, preview a built-in conversion command, run it, inspect its exit status and logs, then use its new output as the next input.",
            ],
            [
              "05 / Share",
              "README.md and README.zh-CN.md contain installation, SSH access, deployment, API, troubleshooting and GitHub release instructions. LEVI preserves the upstream Apache-2.0 license and attribution.",
            ],
          ].map(([heading, body]) => (
            <section className="levi-box" key={heading}>
              <h2>
                <T>{heading}</T>
              </h2>
              <p>
                <T>{body}</T>
              </p>
            </section>
          ))}
        </main>
      }
    </T>
  );
}
