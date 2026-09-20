"use client";
import { useRef, useState } from "react";
import { useLocale } from "./levi-locale";

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
}: {
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
      <p className="levi-agent-muted">
        {emptyHint ?? t("Nothing to choose yet")}
      </p>
    );
  }

  return (
    <div className="levi-chips-field">
      <div className="levi-chips-actions">
        <button
          type="button"
          onClick={() => onChange(options.map((o) => o.value))}
        >
          {t("Select all")}
        </button>
        <button type="button" onClick={() => onChange([])}>
          {t("Clear")}
        </button>
        <span className="levi-agent-muted">
          {selected.length}/{options.length} {t("selected")}
        </span>
      </div>
      <div
        className={`levi-chips ${columns ? "columns" : ""}`}
        role="listbox"
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
            <button
              key={option.value}
              type="button"
              role="option"
              aria-selected={on}
              title={option.hint}
              className={`levi-chip ${on ? "on" : ""}`}
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
            </button>
          );
        })}
      </div>
      {dragging && (
        <p className="levi-agent-muted">{t("Drag across to select a range")}</p>
      )}
    </div>
  );
}
