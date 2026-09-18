"use client";
import Link from "next/link";
import { T } from "@/components/levi-locale";
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
  const { isRaw, entry, format } = useDatasetSource();
  if (!isRaw || !entry) return null;
  const convertHref = `/workbench?source=${encodeURIComponent(entry.path)}`;
  if (compact) {
    return (
      <div className="levi-raw-notice compact" role="note">
        <DatasetFormatBadge format={format ?? undefined} compact />
        <span>
          <T>
            Raw capture shown through a lossless browsing view — every captured
            frame. Annotations and outcome labels made here carry over when you
            convert.
          </T>
        </span>
        <Link className="levi-raw-link" href={convertHref}>
          <T>Convert in the Workbench</T> ↗
        </Link>
      </div>
    );
  }
  return (
    <div className="levi-raw-notice" role="note">
      <div className="levi-raw-notice-head">
        <DatasetFormatBadge format={format ?? undefined} compact />
        <strong>
          <T>
            {feature === "export"
              ? "Exporting needs a converted dataset"
              : feature === "doctor"
                ? "Diagnosing the browsing view of a raw capture"
                : "You are browsing a raw capture"}
          </T>
        </strong>
      </div>
      <p>
        <T>
          {feature === "export"
            ? "A raw capture is shown through a lossless browsing view and is not a training dataset itself. Convert it in the Workbench — language/event annotations, outcome labels and SAM3 objects made here are carried over to the converted dataset, which you can then export."
            : feature === "doctor"
              ? "These checks run on the generated view (every frame kept, retimed losslessly). For the capture itself — CSV schema, frame alignment, camera stalls, completion markers — use Inspect input in the Workbench."
              : "Every captured frame is shown through a lossless browsing view. Viewing, statistics, annotation, SAM3 objects and outcome labels work here and carry over when you convert; exporting requires converting first."}
        </T>
      </p>
      <Link className="levi-secondary inline-block mt-2" href={convertHref}>
        <T>Convert in the Workbench</T> ↗
      </Link>
    </div>
  );
}
