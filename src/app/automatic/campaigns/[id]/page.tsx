"use client";
// /automatic/campaigns/<id>: progress and what needs a person.
import "@/components/pages-ui/pages.css";
import "@/components/automatic/wizard-styles.css";
import "@/components/automatic/campaign-styles.css";
import { useParams } from "next/navigation";
import { CampaignOverview } from "@/components/automatic/campaign-overview";

export default function CampaignPage() {
  const params = useParams<{ id: string }>();
  const id = decodeURIComponent(String(params?.id ?? ""));
  return (
    <main className="ds-root pg-workbench aw-page">
      <CampaignOverview campaignId={id} />
    </main>
  );
}
