"use client";
// /automatic/campaigns/<id>/report?basis=…
import "@/components/pages-ui/pages.css";
import "@/components/automatic/wizard-styles.css";
import "@/components/automatic/campaign-styles.css";
import { Suspense } from "react";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { CampaignReportView } from "@/components/automatic/campaign-report";
import { basisFromQuery } from "@/components/automatic/campaign-report-logic";
import { LABEL_BASES } from "@/components/automatic/wizard-logic";

function ReportPageInner() {
  const params = useParams<{ id: string }>();
  const search = useSearchParams();
  const router = useRouter();
  const id = decodeURIComponent(String(params?.id ?? ""));
  const basis = basisFromQuery(
    search?.get("basis"),
    LABEL_BASES.map((b) => b.value),
    "operator_label",
  );
  return (
    <main className="ds-root pg-workbench aw-page">
      <CampaignReportView
        campaignId={id}
        basis={basis}
        onBasisChange={(next) =>
          router.replace(
            `/automatic/campaigns/${encodeURIComponent(id)}/report?basis=${encodeURIComponent(next)}`,
          )
        }
      />
    </main>
  );
}

export default function CampaignReportPage() {
  // useSearchParams needs a Suspense boundary for the static build.
  return (
    <Suspense fallback={null}>
      <ReportPageInner />
    </Suspense>
  );
}
