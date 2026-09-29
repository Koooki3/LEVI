"use client";
import { useEffect, useId, useState } from "react";
import { useLocale } from "@/components/levi-locale";
import {
  STRATEGIES,
  STRATEGY_LABELS,
  type Strategy,
  type Suggest,
  type TaskEntry,
} from "./types";

export type Share = "natural" | "success" | "failure" | "custom";

/** Python's ``round`` (half to even), so the hint matches the server. */
export function roundHalfEven(x: number): number {
  const floor = Math.floor(x);
  const diff = x - floor;
  if (diff < 0.5) return floor;
  if (diff > 0.5) return floor + 1;
  return floor % 2 === 0 ? floor : floor + 1;
}

export function shareOf(ratio: number | null): Share {
  if (ratio === null) return "natural";
  if (ratio === 1) return "success";
  if (ratio === 0) return "failure";
  return "custom";
}

/** The quota rule of levi/pool/select.py, for a hint before the server
 * answers: how many successes and failures a count and share give, and what
 * is missing when one side has too few. */
export function estimateSplit(
  count: number | null,
  ratio: number | null,
  successes: number,
  failures: number,
  unknown = 0,
) {
  const decided = successes + failures;
  const wanted = Math.min(count ?? decided + unknown, decided + unknown);
  let sWant = 0;
  let fWant = 0;
  if (decided > 0) {
    if (ratio === null) {
      const nd = Math.min(wanted, decided);
      sWant = roundHalfEven((nd * successes) / decided);
      fWant = nd - sWant;
    } else {
      sWant = roundHalfEven(wanted * ratio);
      fWant = wanted - sWant;
    }
  }
  let s = Math.min(sWant, successes);
  let f = Math.min(fWant, failures);
  const shortS = ratio === null ? 0 : sWant - s;
  const shortF = ratio === null ? 0 : fWant - f;
  let lack = wanted - s - f;
  // The side that has enough fills the gap of the other.
  const order =
    sWant > s
      ? (["failure", "success"] as const)
      : (["success", "failure"] as const);
  for (const side of order) {
    const extra = Math.min(
      lack,
      side === "success" ? successes - s : failures - f,
    );
    if (side === "success") s += extra;
    else f += extra;
    lack -= extra;
  }
  return {
    successes: s,
    failures: f,
    unknown: Math.max(0, Math.min(lack, unknown)),
    shortSuccesses: shortS,
    shortFailures: shortF,
  };
}

/** A new entry with the suggested count: null (all) when the suggestion is
 * everything there is. */
export function entryFromSuggest(task: string, info: Suggest): TaskEntry {
  return {
    task,
    count: info.suggested_count >= info.available ? null : info.suggested_count,
    success_ratio: null,
    strategy: "quality",
  };
}

/** Number of episodes, share of successes and how to pick, for one task.
 * Used when adding a task and when editing its row in the composition. */
export function SelectionFields({
  available,
  successes,
  failures,
  unknown = 0,
  value,
  onChange,
}: {
  available: number;
  successes: number;
  failures: number;
  unknown?: number;
  value: TaskEntry;
  onChange: (patch: Partial<TaskEntry>) => void;
}) {
  const { t } = useLocale();
  const id = useId();
  const count = value.count ?? available;
  const [text, setText] = useState(String(count));
  useEffect(() => setText(String(count)), [count]);
  const share = shareOf(value.success_ratio);
  const [custom, setCustom] = useState(
    Math.round((value.success_ratio ?? 0.5) * 100),
  );
  const natural =
    successes + failures > 0
      ? Math.round((successes / (successes + failures)) * 100)
      : null;
  const guess = estimateSplit(
    value.count,
    value.success_ratio,
    successes,
    failures,
    unknown,
  );
  const commit = (n: number) => {
    const bounded = Math.max(1, Math.min(available, Math.floor(n)));
    onChange({ count: bounded >= available ? null : bounded });
  };
  return (
    <div className="levi-pool-picker">
      <div className="levi-pool-picker-count">
        <label htmlFor={`${id}-n`}>
          <span>
            {t("Episodes to take")}{" "}
            <span className="levi-pool-muted">
              ({t("of")} {available.toLocaleString()})
            </span>
          </span>
        </label>
        <div className="levi-pool-picker-row">
          <input
            id={`${id}-n`}
            className="levi-input tabular"
            type="number"
            min={1}
            max={available}
            value={text}
            onChange={(e) => {
              setText(e.target.value);
              const n = Number(e.target.value);
              if (e.target.value !== "" && Number.isFinite(n) && n >= 1)
                commit(n);
            }}
            onBlur={() => setText(String(count))}
          />
          <input
            type="range"
            aria-label={t("Episodes to take")}
            min={1}
            max={available}
            value={count}
            onChange={(e) => commit(Number(e.target.value))}
          />
          <button
            type="button"
            className="levi-pool-link"
            disabled={value.count === null}
            onClick={() => onChange({ count: null })}
          >
            {t("All")}
          </button>
        </div>
      </div>
      <div className="levi-pool-picker-two">
        <label htmlFor={`${id}-share`}>
          <span>{t("Share of successes")}</span>
          <select
            id={`${id}-share`}
            className="levi-input"
            value={share}
            onChange={(e) => {
              const next = e.target.value as Share;
              onChange({
                success_ratio:
                  next === "natural"
                    ? null
                    : next === "success"
                      ? 1
                      : next === "failure"
                        ? 0
                        : custom / 100,
              });
            }}
          >
            <option value="natural">
              {t("Natural share")}
              {natural === null ? "" : ` (${natural}%)`}
            </option>
            <option value="success">{t("All successes")}</option>
            <option value="failure">{t("All failures")}</option>
            <option value="custom">{t("Custom %")}</option>
          </select>
        </label>
        {share === "custom" && (
          <label htmlFor={`${id}-pct`}>
            <span>{t("Successes, %")}</span>
            <input
              id={`${id}-pct`}
              className="levi-input tabular"
              type="number"
              min={0}
              max={100}
              value={custom}
              onChange={(e) => {
                const n = Math.max(
                  0,
                  Math.min(100, Math.round(Number(e.target.value) || 0)),
                );
                setCustom(n);
                onChange({ success_ratio: n / 100 });
              }}
            />
          </label>
        )}
        <label htmlFor={`${id}-how`}>
          <span>{t("How to pick")}</span>
          <select
            id={`${id}-how`}
            className="levi-input"
            value={value.strategy}
            onChange={(e) => onChange({ strategy: e.target.value as Strategy })}
          >
            {STRATEGIES.map((s) => (
              <option key={s} value={s}>
                {t(STRATEGY_LABELS[s])}
              </option>
            ))}
          </select>
        </label>
      </div>
      <p className="levi-pool-hint" aria-live="polite">
        {t("About")} {guess.successes.toLocaleString()} {t("successes")} ·{" "}
        {guess.failures.toLocaleString()} {t("failures")}
        {guess.unknown > 0
          ? ` · ${guess.unknown.toLocaleString()} ${t("without outcome")}`
          : ""}
        {" · "}
        {t("available")}: {successes.toLocaleString()} {t("successes")},{" "}
        {failures.toLocaleString()} {t("failures")}
      </p>
      {(guess.shortSuccesses > 0 || guess.shortFailures > 0) && (
        <p className="levi-pool-warn" role="status">
          {guess.shortSuccesses > 0
            ? t(
                "Not enough successes for the requested share; failures fill in",
              )
            : t(
                "Not enough failures for the requested share; successes fill in",
              )}
        </p>
      )}
      <p className="levi-pool-hint">
        {value.strategy === "quality"
          ? t(
              "Smart pick prefers human-labelled, complete, efficient episodes and spreads them over runs, checkpoints and dates.",
            )
          : value.strategy === "random"
            ? t("Random: a seeded draw, the same every time.")
            : t("In order: the first episodes of the index.")}
      </p>
    </div>
  );
}
