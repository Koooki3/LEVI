"use client";
// "Needs you": what the campaign waits for a person to do. Each is a short
// checklist and one confirmation; the campaign never goes on without it. The
// robot and the policy server are handled by the person, in the terminal.
import { useState } from "react";
import { Button, Checkbox, useConfirm } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { Note, RequestProblem } from "@/components/pages-ui/feedback";
import { ApiError, type WizardApi } from "./wizard-api";
import { confirmKindOf } from "./campaign-logic";
import { IntentKeys } from "./wizard-logic";
import type { CampaignTodo } from "./wizard-types";

const CHECKS: Record<string, string[]> = {
  switch_policy: [
    "automatic.campaign.todo.switch.stopped",
    "automatic.campaign.todo.switch.started",
  ],
  place_cards: [
    "automatic.campaign.todo.cards.placed",
    "automatic.campaign.todo.cards.still",
  ],
  recover_run: ["automatic.campaign.todo.recover.handled"],
};

export function CampaignTodoCard({
  campaignId,
  todo,
  api,
  onDone,
}: {
  campaignId: string;
  todo: CampaignTodo;
  api: Pick<WizardApi, "confirmCampaign">;
  onDone: () => void;
}) {
  const { t } = useLocale();
  const { confirm, dialog } = useConfirm();
  const keys = useState(() => new IntentKeys())[0];
  const kind = todo.kind ?? "recover_run";
  const checks = CHECKS[kind] ?? [];
  const [ticked, setTicked] = useState<boolean[]>(() =>
    checks.map(() => false),
  );
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const requestKind = confirmKindOf(todo);
  const challenge = todo.challenge ?? "";
  const allTicked = ticked.every(Boolean);
  const reason = !requestKind
    ? t("automatic.campaign.todo.unknown")
    : !challenge
      ? t("automatic.campaign.todo.no_challenge")
      : !allTicked
        ? t("automatic.campaign.todo.tick_all")
        : null;

  const send = async () => {
    if (!requestKind || !challenge || busy) return;
    // Two steps: the checklist, then an explicit confirmation.
    const ok = await confirm({
      title: t("automatic.campaign.todo.confirm_title"),
      description: t(
        requestKind === "switch_policy"
          ? "automatic.campaign.todo.confirm_switch"
          : "automatic.campaign.todo.confirm_env",
      ),
      confirmLabel: t("automatic.campaign.todo.confirm_button"),
    });
    if (!ok) return;
    const intent = `confirm:${requestKind}:${challenge}`;
    setBusy(true);
    setProblem(null);
    setNotice(null);
    try {
      const result = await api.confirmCampaign(
        campaignId,
        keys.idFor(intent),
        requestKind,
        challenge,
      );
      if (result.result === "refused") {
        setProblem(result.code ?? "refused");
        keys.release(intent);
      } else {
        keys.release(intent);
        setNotice(
          result.result === "repeated"
            ? t("automatic.campaign.todo.repeated")
            : t("automatic.campaign.todo.applied"),
        );
        onDone();
      }
    } catch (error) {
      setProblem(error instanceof Error ? error.message : String(error));
      if (error instanceof ApiError && error.status !== 0) keys.release(intent);
    } finally {
      setBusy(false);
    }
  };

  const heading =
    kind === "switch_policy"
      ? t("automatic.campaign.todo.switch.title").replace(
          "{arm}",
          todo.arm_code ?? "?",
        )
      : kind === "place_cards"
        ? t("automatic.campaign.todo.cards.title")
        : t("automatic.campaign.todo.recover.title");

  return (
    <section className="ac-todo" aria-labelledby="ac-todo-title" role="region">
      <h2 id="ac-todo-title">{t("automatic.campaign.todo.needs_you")}</h2>
      <p>
        <strong>{heading}</strong>
      </p>
      {todo.detail && <p className="pg-pool-hint">{todo.detail}</p>}
      {kind === "place_cards" && todo.cards && todo.cards.length > 0 && (
        <p>
          {t("automatic.campaign.todo.cards.which")}:{" "}
          {todo.cards.map((c) => (
            <code key={c} className="ac-chip">
              {c}
            </code>
          ))}
        </p>
      )}
      {kind === "recover_run" && (
        <Note tone="warning">{t("automatic.campaign.todo.recover.note")}</Note>
      )}
      {checks.map((key, index) => (
        <Checkbox
          key={key}
          label={t(key)}
          checked={ticked[index]}
          onChange={(event) =>
            setTicked((prev) =>
              prev.map((v, i) => (i === index ? event.target.checked : v)),
            )
          }
        />
      ))}
      <div className="pg-row">
        <Button
          variant="primary"
          loading={busy}
          disabled={reason !== null && !busy}
          onClick={() => void send()}
        >
          {t("automatic.campaign.todo.confirm_button")}
        </Button>
      </div>
      {reason && (
        <p className="aw-step__blocked" role="note">
          {reason}
        </p>
      )}
      {problem && (
        <RequestProblem
          action="automatic.campaign.todo.failed"
          message={problem}
        />
      )}
      {notice && <p role="status">{notice}</p>}
      {dialog}
    </section>
  );
}
