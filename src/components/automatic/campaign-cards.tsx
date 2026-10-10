"use client";
// Guided campaigns: which layout card each episode of the legacy client had.
// The server lists the episodes that wait and suggests a card; a person says
// which card it really was, or that there was none (the episode deviated).
// An episode without a confirmed card never enters the paired analysis.
import { useRef, useState } from "react";
import { Badge, Button, Select, Table } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { Note, RequestProblem } from "@/components/pages-ui/feedback";
import { NO_CARD, cardChoices } from "./campaign-logic";
import { usePolled } from "./campaign-poll";
import { wizardApi, type WizardApi } from "./wizard-api";
import { failureText } from "./wizard-errors";
import { IntentKeys } from "./wizard-logic";
import type { PendingCardEpisode } from "./wizard-types";

type CardsApi = Pick<WizardApi, "getCampaignCards" | "confirmCampaignCard">;

export function CampaignCards({
  campaignId,
  knownCards,
  unconfirmed,
  api = wizardApi,
  intervalMs = 5000,
}: {
  campaignId: string;
  /** Other cards the page knows of (the current segment's). */
  knownCards: readonly string[];
  /** Episodes without a confirmed card, from the arm counts. */
  unconfirmed: number;
  api?: CardsApi;
  intervalMs?: number;
}) {
  const { t } = useLocale();
  const { value, refresh } = usePolled(
    () => api.getCampaignCards(campaignId),
    intervalMs,
  );
  const keys = useRef(new IntentKeys());
  const [choice, setChoice] = useState<Record<string, string>>({});
  const [saved, setSaved] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  const pending: PendingCardEpisode[] = value?.pending ?? [];
  // Nothing to say before the first answer, or when nothing waits.
  if (!value) return null;
  if (pending.length === 0 && unconfirmed === 0) return null;

  const all = [
    ...knownCards,
    ...pending.map((p) => p.candidate_card).filter((c): c is string => !!c),
  ];

  const save = async (episode: PendingCardEpisode) => {
    const picked = choice[episode.key] ?? episode.candidate_card ?? "";
    if (!picked || busy) return;
    const card = picked === NO_CARD ? null : picked;
    const intent = `card:${episode.key}:${picked}`;
    setBusy(episode.key);
    setProblem(null);
    try {
      const result = await api.confirmCampaignCard(
        campaignId,
        keys.current.idFor(intent),
        episode.key,
        card,
      );
      if (result.result !== "refused") {
        setSaved((prev) => ({ ...prev, [episode.key]: picked }));
        refresh();
      } else setProblem(t("automatic.campaign.refused"));
      keys.current.release(intent);
    } catch (e) {
      setProblem(failureText(e, t));
      keys.current.release(intent);
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="aw-panel ac-cards" aria-labelledby="ac-cards">
      <h2 id="ac-cards">{t("automatic.campaign.cards.title")}</h2>
      <Note tone="info">{t("automatic.campaign.cards.note")}</Note>
      {unconfirmed > 0 && (
        <p role="status">
          <Badge tone="warning">
            {t("automatic.campaign.cards.unconfirmed").replace(
              "{n}",
              String(unconfirmed),
            )}
          </Badge>{" "}
          {t("automatic.campaign.cards.not_paired")}
        </p>
      )}
      {pending.length > 0 && (
        <Table caption={t("automatic.campaign.cards.title")} density="compact">
          <thead>
            <tr>
              <th scope="col">{t("automatic.campaign.cards.episode")}</th>
              <th scope="col">{t("automatic.campaign.cards.suggested")}</th>
              <th scope="col">{t("automatic.campaign.cards.card")}</th>
              <th scope="col">
                <span className="ds-sr-only">
                  {t("automatic.campaign.cards.save")}
                </span>
              </th>
            </tr>
          </thead>
          <tbody>
            {pending.map((p) => {
              const picked = choice[p.key] ?? p.candidate_card ?? "";
              const label = t("automatic.campaign.cards.episode_label")
                .replace("{segment}", String(p.segment))
                .replace("{number}", String(p.number));
              return (
                <tr key={p.key}>
                  <th scope="row">{label}</th>
                  <td>
                    {p.candidate_card ? (
                      <code className="ac-chip">{p.candidate_card}</code>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td>
                    <Select
                      aria-label={`${label}: ${t("automatic.campaign.cards.card")}`}
                      value={picked}
                      onChange={(e) =>
                        setChoice((prev) => ({
                          ...prev,
                          [p.key]: e.target.value,
                        }))
                      }
                    >
                      <option value="">
                        {t("automatic.campaign.cards.choose")}
                      </option>
                      {cardChoices(p.candidate_card, all).map((c) => (
                        <option key={c} value={c}>
                          {c}
                        </option>
                      ))}
                      <option value={NO_CARD}>
                        {t("automatic.campaign.cards.none")}
                      </option>
                    </Select>
                  </td>
                  <td>
                    <Button
                      size="sm"
                      loading={busy === p.key}
                      disabled={!picked || busy !== null}
                      onClick={() => void save(p)}
                    >
                      {t("automatic.campaign.cards.save")}
                    </Button>{" "}
                    {saved[p.key] !== undefined && saved[p.key] === picked && (
                      <Badge tone="success">
                        {picked === NO_CARD
                          ? t("automatic.campaign.cards.saved_none")
                          : t("automatic.campaign.cards.saved")}
                      </Badge>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </Table>
      )}
      {pending.length > 0 && (
        <p className="pg-pool-hint">
          {t("automatic.campaign.cards.pick_hint")}
        </p>
      )}
      {problem && (
        <RequestProblem
          action="automatic.campaign.cards.failed"
          message={problem}
        />
      )}
    </section>
  );
}
