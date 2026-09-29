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
