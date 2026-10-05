"use client";
import { useRef, useState } from "react";
import { useLocale } from "./levi-locale";
import { Button } from "@/components/ds";
import "@/components/pages-ui/agent-content.css";

export type ChipOption = { value: string; label: string; hint?: string };

/** Pick from a known set by clicking, dragging across, or selecting all.
 *
 * Episode numbers, camera keys and task strings are all facts the dataset
 * already states. Typing them invites a typo that only surfaces as a failed
 * plan, so the workbench offers what exists and lets the person choose.
 * Dragging selects a run of chips, which is how episode ranges are picked.
 */
export default function ChipMultiSelect({
  options,
  selected,
  onChange,
  emptyHint,
  columns,
  label,
}: {
  /** The name of the list for assistive technology ("Episodes"). */
  label: string;
  options: ChipOption[];
  selected: string[];
  onChange: (next: string[]) => void;
  emptyHint?: string;
  columns?: boolean;
}) {
  const { t } = useLocale();
  const chosen = new Set(selected);
  const anchor = useRef<number | null>(null);
  const adding = useRef(true);
  const [dragging, setDragging] = useState(false);

  function applyRange(from: number, to: number, add: boolean) {
    const [start, end] = from <= to ? [from, to] : [to, from];
    const next = new Set(selected);
    for (let index = start; index <= end; index += 1) {
      const value = options[index]?.value;
      if (value === undefined) continue;
      if (add) next.add(value);
      else next.delete(value);
    }
    onChange(
      options.filter((option) => next.has(option.value)).map((o) => o.value),
    );
  }

  if (options.length === 0) {
    return (
      <p className="ag-muted">{emptyHint ?? t("Nothing to choose yet")}</p>
    );
  }

  return (
    <div className="ag-chips-field">
      <div className="ag-chips-actions">
        <Button
          size="sm"
          variant="ghost"
          onClick={() => onChange(options.map((o) => o.value))}
        >
          {t("Select all")}
        </Button>
        <Button size="sm" variant="ghost" onClick={() => onChange([])}>
          {t("Clear")}
        </Button>
        <span className="ag-muted" aria-live="polite">
          {selected.length}/{options.length} {t("selected")}
        </span>
      </div>
      <div
        className={`ag-chips ${columns ? "is-columns" : ""}`}
        role="listbox"
        aria-label={label}
        aria-multiselectable
        onPointerUp={() => {
          anchor.current = null;
          setDragging(false);
        }}
        onPointerLeave={() => {
          anchor.current = null;
          setDragging(false);
        }}
      >
        {options.map((option, index) => {
          const on = chosen.has(option.value);
          return (
            // A chip is a ds Button used as a listbox option: selected is the
            // filled (primary) look, not selected the outlined one. Title is
            // kept for options whose label is cut short (a long task text).
            <Button
              key={option.value}
              size="sm"
              variant={on ? "primary" : "secondary"}
              role="option"
              aria-selected={on}
              title={option.hint}
              className="ag-chip"
              onPointerDown={(event) => {
                event.preventDefault();
                anchor.current = index;
                adding.current = !on;
                setDragging(true);
                applyRange(index, index, !on);
              }}
              onPointerEnter={() => {
                if (anchor.current === null) return;
                applyRange(anchor.current, index, adding.current);
              }}
              onKeyDown={(event) => {
                if (event.key === " " || event.key === "Enter") {
                  event.preventDefault();
                  applyRange(index, index, !on);
                }
              }}
            >
              {option.label}
            </Button>
          );
        })}
      </div>
      {dragging && (
        <p className="ag-muted">{t("Drag across to select a range")}</p>
      )}
    </div>
  );
}
