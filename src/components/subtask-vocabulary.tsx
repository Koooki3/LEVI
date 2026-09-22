"use client";
/**
 * The dataset's subtask vocabulary in the human editor.
 *
 * With a vocabulary, a person tags each subtask span with a subtask id and
 * its outcome — the same `levi.subtask_id` / `levi.outcome` an agent writes —
 * so human and agent annotations compare interval by interval. Without one
 * the editor works exactly as before (free text).
 */
import React, { useState } from "react";
import type { LanguageAtom } from "../types/language.types";
import {
  saveVocabulary,
  type DatasetIdent,
  type SubtaskTerm,
  type Vocabulary,
} from "../utils/annotationsClient";
import { T, useLocale } from "./levi-locale";

export type SubtaskTag = {
  subtask_id?: string | null;
  outcome?: "success" | "failure" | "unknown" | null;
};

const OUTCOMES = ["success", "failure", "unknown"] as const;

/** `levi` for an atom with this tag, keeping whatever else it carries. */
export function withTag(
  levi: LanguageAtom["levi"],
  tag: SubtaskTag,
): LanguageAtom["levi"] {
  const next: Record<string, unknown> = { ...(levi || {}) };
  for (const key of ["subtask_id", "outcome"] as const) {
    if (tag[key]) next[key] = tag[key];
    else delete next[key];
  }
  return Object.keys(next).length ? (next as LanguageAtom["levi"]) : null;
}

export function SubtaskTagFields({
  vocabulary,
  value,
  onChange,
}: {
  vocabulary: Vocabulary;
  value: SubtaskTag;
  onChange: (tag: SubtaskTag) => void;
}) {
  const { t } = useLocale();
  if (!vocabulary.subtasks.length) return null;
  const special = vocabulary.special ?? ["other", "unknown"];
  return (
    <>
      <select
        aria-label={t("Subtask")}
        value={value.subtask_id ?? ""}
        onChange={(e) =>
          onChange({ ...value, subtask_id: e.target.value || null })
        }
      >
        <option value="">{t("subtask…")}</option>
        {vocabulary.subtasks.map((term) => (
          <option key={term.id} value={term.id} title={term.definition}>
            {term.label}
          </option>
        ))}
        {special.map((id) => (
          <option key={id} value={id}>
            {id}
          </option>
        ))}
      </select>
      <select
        aria-label={t("Outcome")}
        value={value.outcome ?? ""}
        onChange={(e) =>
          onChange({
            ...value,
            outcome: (e.target.value || null) as SubtaskTag["outcome"],
          })
        }
      >
        <option value="">{t("outcome…")}</option>
        {OUTCOMES.map((o) => (
          <option key={o} value={o}>
            {t(o)}
          </option>
        ))}
      </select>
    </>
  );
}

function parse(text: string): SubtaskTerm[] {
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const [id, label, ...rest] = line.split("|").map((part) => part.trim());
      return { id, label: label || id, definition: rest.join(" | ") };
    });
}

function format(terms: SubtaskTerm[]): string {
  return terms
    .map((term) =>
      [term.id, term.label, term.definition].filter(Boolean).join(" | "),
    )
    .join("\n");
}

export function VocabularyEditor({
  ident,
  vocabulary,
  onSaved,
}: {
  ident: DatasetIdent;
  vocabulary: Vocabulary;
  onSaved: (vocabulary: Vocabulary) => void;
}) {
  const [text, setText] = useState(() => format(vocabulary.subtasks));
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  React.useEffect(() => {
    setText(format(vocabulary.subtasks));
  }, [vocabulary.subtasks]);
  const suggested = vocabulary.suggested ?? [];
  return (
    <details className="vocab-editor">
      <summary>
        <T>Subtask vocabulary</T> ({vocabulary.subtasks.length})
      </summary>
      <p className="levi-agent-muted">
        <T>
          One subtask per line: id | label | definition. With a vocabulary each
          subtask span gets an id and an outcome, comparable with agent
          annotations.
        </T>
      </p>
      <textarea
        rows={Math.max(4, text.split("\n").length + 1)}
        value={text}
        placeholder="grasp | grasp | close the fingers on the plate rim"
        onChange={(e) => setText(e.target.value)}
      />
      <div className="vocab-actions">
        {!vocabulary.subtasks.length && suggested.length > 0 && (
          <button type="button" onClick={() => setText(format(suggested))}>
            <T>{"Use the last agent plan's subtasks"}</T> ({suggested.length})
          </button>
        )}
        <button
          type="button"
          disabled={busy}
          onClick={async () => {
            setBusy(true);
            setError("");
            try {
              onSaved(await saveVocabulary(ident, parse(text)));
            } catch (e) {
              setError(e instanceof Error ? e.message : String(e));
            } finally {
              setBusy(false);
            }
          }}
        >
          <T>Save vocabulary</T>
        </button>
        {error && (
          <span className="levi-error" role="alert">
            {error}
          </span>
        )}
      </div>
    </details>
  );
}
