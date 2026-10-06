"use client";
import Link from "next/link";
import { T } from "@/components/levi-locale";
import { ArrowUpRight, Info } from "lucide-react";
import { Icon } from "@/components/ds";
import "@/components/viewer/viewer.css";
import { DatasetFormatBadge } from "@/components/dataset-format";
import { useDatasetSource } from "@/context/dataset-source-context";

/** Shown on a raw capture's pages: what the view is, what works, and the
 * way forward for what doesn't (convert — annotations carry over). */
export function RawCaptureNotice({
  feature,
  compact = false,
}: {
  feature?: "export" | "doctor";
  compact?: boolean;
}) {
  const { isRaw, entry, format, linked } = useDatasetSource();
  // A live workspace's dataset is not converted from here: its own notice
  // (LinkedDatasetNotice) says what it is.
  if (!isRaw || !entry || linked) return null;
  const convertHref = `/workbench?source=${encodeURIComponent(entry.path)}`;
  const isDroid = format?.input_format === "droid_raw";
  if (compact) {
    return (
      <div className="vw-note vw-note--compact" role="note">
        <Icon icon={Info} />
        <DatasetFormatBadge format={format ?? undefined} compact />
        <span>
          <T>
            {isDroid
              ? "DROID is browsed and annotated through a stream-copied, nominal-clock view. Original control timestamps remain in provenance; training conversion needs an explicit mapping."
              : "Raw capture shown through a lossless browsing view — every captured frame. Annotations and outcome labels made here carry over when you convert."}
          </T>
        </span>
        {!isDroid && (
          <Link className="vw-link vw-note-action" href={convertHref}>
            <T>Convert in the Workbench</T>
            <Icon icon={ArrowUpRight} />
          </Link>
        )}
      </div>
    );
  }
  return (
    <div className="vw-note" role="note">
      <div className="vw-note-head">
        <Icon icon={Info} />
        <DatasetFormatBadge format={format ?? undefined} compact />
        <strong>
          <T>
            {isDroid
              ? "You are browsing a DROID raw capture"
              : feature === "export"
                ? "Exporting needs a converted dataset"
                : feature === "doctor"
                  ? "Diagnosing the browsing view of a raw capture"
                  : "You are browsing a raw capture"}
          </T>
        </strong>
      </div>
      <p>
        <T>
          {isDroid
            ? "DROID raw supports browsing and annotation, but this release does not write a DROID training-format conversion. The view uses nominal video time; check the preserved source timestamps before precise boundary claims."
            : feature === "export"
              ? "A raw capture is shown through a lossless browsing view and is not a training dataset itself. Convert it in the Workbench — language/event annotations, outcome labels and SAM3 objects made here are carried over to the converted dataset, which you can then export."
              : feature === "doctor"
                ? "These checks run on the generated view (every frame kept, retimed losslessly). For the capture itself — CSV schema, frame alignment, camera stalls, completion markers — use Inspect input in the Workbench."
                : "Every captured frame is shown through a lossless browsing view. Viewing, statistics, annotation, SAM3 objects and outcome labels work here and carry over when you convert; exporting requires converting first."}
        </T>
      </p>
      {!isDroid && (
        <Link
          className="ds-btn ds-btn--secondary ds-btn--sm ds-focus mt-2"
          href={convertHref}
        >
          <T>Convert in the Workbench</T>
          <Icon icon={ArrowUpRight} />
        </Link>
      )}
    </div>
  );
}
