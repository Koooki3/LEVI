"use client";
import { useState } from "react";
import { Button, Field, Input } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";

/** A button that works only after a person has typed the confirmation phrase
 * (`launch`, `start-campaign`, `unblind`...). The phrase is what the request
 * carries; typing it is the person's deliberate act. */
export function PhraseConfirm({
  phrase,
  label,
  hint,
  buttonLabel,
  busy = false,
  disabled = false,
  disabledReason,
  variant = "primary",
  onConfirm,
}: {
  phrase: string;
  label: string;
  hint?: string;
  buttonLabel: string;
  busy?: boolean;
  disabled?: boolean;
  /** Visible reason the button cannot be used (never only a tooltip). */
  disabledReason?: string;
  variant?: "primary" | "danger";
  onConfirm: () => void;
}) {
  const { t } = useLocale();
  const [typed, setTyped] = useState("");
  const matches = typed.trim() === phrase;
  const ready = matches && !disabled && !busy;
  return (
    <div className="aw-phrase">
      <Field label={label} hint={hint}>
        <Input
          value={typed}
          autoComplete="off"
          spellCheck={false}
          onChange={(event) => setTyped(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && ready) {
              event.preventDefault();
              onConfirm();
            }
          }}
        />
      </Field>
      <Button
        variant={variant}
        loading={busy}
        disabled={!ready && !busy}
        onClick={() => {
          if (ready) onConfirm();
        }}
      >
        {buttonLabel}
      </Button>
      {disabled && disabledReason ? (
        <p className="aw-step__blocked" role="note">
          {disabledReason}
        </p>
      ) : !matches && typed ? (
        <p className="pg-pool-hint" role="note">
          {t("automatic.wizard.phrase.mismatch").replace("{phrase}", phrase)}
        </p>
      ) : null}
    </div>
  );
}
