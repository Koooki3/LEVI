"use client";
import { T, useLocale } from "@/components/levi-locale";
import { Check, Wand2 } from "lucide-react";
import { Badge, Button } from "@/components/ds";
import { useServerText } from "@/components/pages-ui/messages";
import type { Solution, TargetCompatibility } from "./types";

const STATUS = {
  supported: "success",
  warnings: "warning",
  unsupported: "danger",
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
  const serverText = useServerText();
  return (
    <div className="pg-cards">
      {targets.map((target) => {
        const usable = target.status !== "unsupported";
        return (
          <div
            key={target.target}
            className={`pg-card ${selected === target.target ? "selected" : ""}`}
          >
            <div className="pg-row pg-between">
              <h3>{t(target.label)}</h3>
              <Badge tone={STATUS[target.status]}>{t(target.status)}</Badge>
            </div>
            {target.reasons.length > 0 && (
              <ul className="pg-reasons">
                {target.reasons.map((reason) => (
                  <li key={reason}>{serverText(reason)}</li>
                ))}
              </ul>
            )}
            {Object.keys(target.defaults).length > 0 && (
              <p className="pg-small">
                <T>Defaults for this target</T>:{" "}
                <code>
                  {Object.entries(target.defaults)
                    .map(([k, v]) => `${k}=${String(v)}`)
                    .join(", ")}
                </code>
              </p>
            )}
            {target.solutions.length > 0 && (
              <div className="pg-row pg-mt-3">
                {target.solutions.map((solution) => (
                  <Button
                    key={solution.id}
                    size="sm"
                    icon={Wand2}
                    onClick={() => onSolution(solution)}
                  >
                    {t(solution.label)}
                  </Button>
                ))}
              </div>
            )}
            <Button
              className="pg-mt-4 pg-choice"
              icon={selected === target.target ? Check : undefined}
              disabled={!usable}
              aria-pressed={selected === target.target}
              onClick={() => onSelect(target)}
            >
              <T>{usable ? "Choose this export" : "Not available"}</T>
            </Button>
          </div>
        );
      })}
    </div>
  );
}
