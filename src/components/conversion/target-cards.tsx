"use client";
import { T, useLocale } from "@/components/levi-locale";
import type { Solution, TargetCompatibility } from "./types";

const STATUS = {
  supported: "pass",
  warnings: "warn",
  unsupported: "fail",
} as const;

/** One card per output format: can this input be exported, why not, and
 * one-click fixes (option changes, or a link to label outcomes). */
export function TargetCards({
  targets,
  selected,
  onSelect,
  onSolution,
}: {
  targets: TargetCompatibility[];
  selected: string | null;
  onSelect: (target: TargetCompatibility) => void;
  onSolution: (solution: Solution) => void;
}) {
  const { t } = useLocale();
  return (
    <div className="levi-cards">
      {targets.map((target) => {
        const usable = target.status !== "unsupported";
        return (
          <div
            key={target.target}
            className={`levi-card ${selected === target.target ? "selected" : ""}`}
          >
            <div className="levi-row justify-between">
              <h3>{t(target.label)}</h3>
              <span className={`levi-status ${STATUS[target.status]}`}>
                {t(target.status)}
              </span>
            </div>
            {target.reasons.length > 0 && (
              <ul className="levi-reasons">
                {target.reasons.map((reason) => (
                  <li key={reason}>{t(reason)}</li>
                ))}
              </ul>
            )}
            {Object.keys(target.defaults).length > 0 && (
              <p className="text-xs">
                <T>Defaults for this target</T>:{" "}
                <code>
                  {Object.entries(target.defaults)
                    .map(([k, v]) => `${k}=${String(v)}`)
                    .join(", ")}
                </code>
              </p>
            )}
            {target.solutions.length > 0 && (
              <div className="levi-row mt-3">
                {target.solutions.map((solution) => (
                  <button
                    key={solution.id}
                    type="button"
                    className="levi-secondary"
                    onClick={() => onSolution(solution)}
                  >
                    {t(solution.label)}
                  </button>
                ))}
              </div>
            )}
            <button
              type="button"
              className="levi-primary mt-4"
              disabled={!usable}
              onClick={() => onSelect(target)}
            >
              <T>{usable ? "Choose this export" : "Not available"}</T>
            </button>
          </div>
        );
      })}
    </div>
  );
}
