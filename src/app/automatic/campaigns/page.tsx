"use client";
// /automatic/campaigns: the multi-model evaluation plans on this machine.
import "@/components/pages-ui/pages.css";
import "@/components/automatic/wizard-styles.css";
import "@/components/automatic/campaign-styles.css";
import { CampaignList } from "@/components/automatic/campaign-list";

export default function CampaignsPage() {
  return (
    <main className="ds-root pg-workbench aw-page">
      <CampaignList />
    </main>
  );
}
