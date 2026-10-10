"use client";
import Link from "next/link";
import { Plus } from "lucide-react";
import { Badge, EmptyState, Table } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { RequestProblem } from "@/components/pages-ui/feedback";
import { STATE_TONE, viewOf } from "./campaign-logic";
import { usePolled } from "./campaign-poll";
import { wizardApi, type WizardApi } from "./wizard-api";

/** The campaigns on this machine: state and progress, no results. */
export function CampaignList({
  api = wizardApi,
}: {
  api?: Pick<WizardApi, "listCampaigns">;
}) {
  const { t } = useLocale();
  const { value, error, refresh } = usePolled(() => api.listCampaigns(), 5000);
  const rows = (value?.campaigns ?? []).map(viewOf);
  return (
    <>
      <header className="pg-head">
        <h1>{t("automatic.campaign.list.title")}</h1>
        <div className="pg-head-actions">
          <Link
            className="ds-btn ds-btn--primary ds-btn--md"
            href="/automatic/new"
          >
            <Plus size={16} aria-hidden="true" />{" "}
            {t("automatic.campaign.list.new")}
          </Link>
        </div>
      </header>
      {error && !value && (
        <RequestProblem
          action="automatic.campaign.load_failed"
          message={error}
          onRetry={refresh}
        />
      )}
      {value && rows.length === 0 && (
        <EmptyState title={t("automatic.campaign.list.empty")} />
      )}
      {rows.length > 0 && (
        <Table caption={t("automatic.campaign.list.title")}>
          <thead>
            <tr>
              <th scope="col">{t("automatic.campaign.list.id")}</th>
              <th scope="col">{t("automatic.campaign.list.state")}</th>
              <th scope="col">{t("automatic.campaign.list.progress")}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.id}>
                <th scope="row">
                  <Link
                    href={`/automatic/campaigns/${encodeURIComponent(row.id)}`}
                  >
                    {row.id}
                  </Link>
                </th>
                <td>
                  <Badge tone={STATE_TONE[row.state] ?? "neutral"}>
                    {row.state}
                  </Badge>
                </td>
                <td className="ds-num">
                  {row.done} / {row.total}
                </td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
    </>
  );
}
