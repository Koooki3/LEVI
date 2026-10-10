"use client";
// "Needs you": what the campaign waits for a person to do. Each is a short
// checklist and one confirmation; the campaign never goes on without it. The
// robot and the policy server are handled by the person, in the terminal.
import { useState } from "react";
import { Button, Checkbox, useConfirm } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { Note, RequestProblem } from "@/components/pages-ui/feedback";
import { ApiError, type WizardApi } from "./wizard-api";
import { codeKey, failureText } from "./wizard-errors";
import { confirmKindOf, isKnownTodo, reasonKey } from "./campaign-logic";
import { SetupCommand } from "./setup-copy";
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
  segment_done: ["automatic.campaign.todo.done.finished"],
  recover_run: ["automatic.campaign.todo.recover.handled"],
};

/** What a person may add when answering a campaign that waits after a fault
 * or a short segment: none is ticked by default. `when` limits an option to
 * the reasons it answers. */
const RECOVER_OPTIONS: {
  name: string;
  key: string;
  when: (reason: string) => boolean;
}[] = [
  {
    name: "accept_short_segment",
    key: "automatic.campaign.todo.recover.accept_short",
    when: (r) => r === "segment_short",
  },
  {
    name: "override_stop_rule",
    key: "automatic.campaign.todo.recover.override_stop",
    when: (r) => r.startsWith("stop_rule_"),
  },
  {
    name: "relaunch",
    key: "automatic.campaign.todo.recover.relaunch",
    when: () => true,
  },
];

const CONFIRM_TEXT: Record<string, string> = {
  switch_policy: "automatic.campaign.todo.confirm_switch",
  segment_done: "automatic.campaign.todo.confirm_done",
  env: "automatic.campaign.todo.confirm_env",
};

export function CampaignTodoCard({
  campaignId,
  todo,
  api,
  onDone,
  controllerDown = false,
}: {
  campaignId: string;
  todo: CampaignTodo;
  api: Pick<WizardApi, "confirmCampaign">;
  onDone: () => void;
  /** The controller is not running: a confirmation would be refused. */
  controllerDown?: boolean;
}) {
  const { t } = useLocale();
  const { confirm, dialog } = useConfirm();
  const keys = useState(() => new IntentKeys())[0];
  const known = isKnownTodo(todo);
  const kind = known ? todo.kind : "unknown";
  const checks = CHECKS[kind] ?? [];
  const waitReason = todo.reason ?? "";
  const options = kind === "recover_run" ? RECOVER_OPTIONS : [];
  const [chosen, setChosen] = useState<Record<string, boolean>>({});
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
    : controllerDown
      ? t("automatic.campaign.todo.controller_down")
      : !challenge
        ? t("automatic.campaign.todo.no_challenge")
        : !allTicked
          ? t("automatic.campaign.todo.tick_all")
          : null;

  const send = async () => {
    if (!requestKind || !challenge || busy || controllerDown) return;
    // Two steps: the checklist, then an explicit confirmation.
    const ok = await confirm({
      title: t("automatic.campaign.todo.confirm_title"),
      description: t(CONFIRM_TEXT[requestKind]),
      confirmLabel: t("automatic.campaign.todo.confirm_button"),
    });
    if (!ok) return;
    const sent = Object.fromEntries(
      Object.entries(chosen).filter(([, on]) => on),
    );
    const intent = `confirm:${requestKind}:${challenge}:${JSON.stringify(sent)}`;
    setBusy(true);
    setProblem(null);
    setNotice(null);
    try {
      const result = await api.confirmCampaign(
        campaignId,
        keys.idFor(intent),
        requestKind,
        challenge,
        kind === "recover_run" ? sent : undefined,
      );
      if (result.result === "refused") {
        const key = result.code ? codeKey(result.code) : null;
        setProblem(t(key ?? "automatic.campaign.refused"));
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
      setProblem(failureText(error, t));
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
        : kind === "segment_done"
          ? t("automatic.campaign.todo.done.title").replace(
              "{arm}",
              todo.arm_code ?? "?",
            )
          : kind === "recover_run"
            ? t("automatic.campaign.todo.recover.title")
            : t("automatic.campaign.todo.other.title");

  return (
    <section className="ac-todo" aria-labelledby="ac-todo-title" role="region">
      <h2 id="ac-todo-title">{t("automatic.campaign.todo.needs_you")}</h2>
      <p>
        <strong>{heading}</strong>
      </p>
      {/* The server's own sentence is English: it is shown only for a kind
          this page does not know, where it is all there is to say. */}
      {kind === "unknown" && todo.detail && (
        <p className="pg-pool-hint">{todo.detail}</p>
      )}
      {kind === "switch_policy" && (todo.checkpoint || todo.config) && (
        <p className="pg-pool-hint">
          {t("automatic.campaign.todo.switch.which")
            .replace("{checkpoint}", todo.checkpoint ?? "?")
            .replace("{config}", todo.config ?? "?")}
        </p>
      )}
      {(kind === "place_cards" || kind === "segment_done") &&
        todo.cards &&
        todo.cards.length > 0 && (
          <p>
            {t("automatic.campaign.todo.cards.which")}:{" "}
            {todo.cards.map((c) => (
              <code key={c} className="ac-chip">
                {c}
              </code>
            ))}
          </p>
        )}
      {(kind === "place_cards" || kind === "segment_done") && todo.command && (
        <div className="ac-command">
          <p className="pg-pool-hint">
            {t("automatic.campaign.todo.command.hint")}
          </p>
          <SetupCommand
            command={todo.command}
            label={t("automatic.campaign.todo.command.label")}
          />
        </div>
      )}
      {kind === "segment_done" && todo.progress && (
        <p role="status">
          {t("automatic.campaign.todo.done.progress")
            .replace("{done}", String(todo.progress.done))
            .replace("{planned}", String(todo.progress.planned))}
          {todo.pending_cards && todo.pending_cards.length > 0 && (
            <>
              {" · "}
              {t("automatic.campaign.todo.done.cards_pending").replace(
                "{n}",
                String(todo.pending_cards.length),
              )}
            </>
          )}
        </p>
      )}
      {kind === "recover_run" && (
        <>
          <Note tone="warning">
            {t("automatic.campaign.todo.recover.note")}
          </Note>
          <p className="pg-pool-hint">
            {t("automatic.campaign.todo.recover.why").replace(
              "{reason}",
              t(reasonKey(waitReason)),
            )}
          </p>
        </>
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
      {options
        .filter((o) => o.when(waitReason))
        .map((o) => (
          <Checkbox
            key={o.name}
            label={t(o.key)}
            checked={chosen[o.name] === true}
            onChange={(event) =>
              setChosen((prev) => ({ ...prev, [o.name]: event.target.checked }))
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
