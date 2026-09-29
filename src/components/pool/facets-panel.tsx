"use client";
import { useState } from "react";
import { useLocale } from "@/components/levi-locale";
import {
  CATEGORIES,
  CATEGORY_LABELS,
  type Facets,
  type OutcomeFilter,
} from "./types";

export interface Filters {
  categories: string[];
  sources: string[];
  search: string;
  outcome: OutcomeFilter;
  policies: string[];
  dateFrom: string;
  dateTo: string;
  showHeldout: boolean;
  showCopies: boolean;
  showArchive: boolean;
}

export const EMPTY_FILTERS: Filters = {
  categories: [],
  sources: [],
  search: "",
  outcome: "all",
  policies: [],
  dateFrom: "",
  dateTo: "",
  showHeldout: false,
  showCopies: false,
  showArchive: false,
};

function toggle(list: string[], value: string): string[] {
  return list.includes(value)
    ? list.filter((v) => v !== value)
    : [...list, value];
}

function shortSource(source: string): string {
  const parts = source.split("/");
  return parts.length > 3 ? `…/${parts.slice(-3).join("/")}` : source;
}

/** Left column: category, source, task search, outcome, policy and date
 * facets with counts; held-out, copies and archive are hidden by default. */
export function FacetsPanel({
  facets,
  filters,
  onChange,
}: {
  facets: Facets | null;
  filters: Filters;
  onChange: (next: Filters) => void;
}) {
  const { t } = useLocale();
  const [sourceSearch, setSourceSearch] = useState("");
  const [allSources, setAllSources] = useState(false);
  const set = (patch: Partial<Filters>) => onChange({ ...filters, ...patch });
  const sources = (facets?.sources || []).filter(
    (s) =>
      !sourceSearch ||
      s.source.toLowerCase().includes(sourceSearch.toLowerCase()),
  );
  const shownSources = allSources ? sources : sources.slice(0, 12);
  const policies = Object.entries(facets?.policies || {}).sort(
    (a, b) => b[1] - a[1],
  );
  const categories = CATEGORIES.filter(
    (c) => c !== "archive" || filters.showArchive,
  );
  return (
    <aside className="levi-pool-facets" aria-label={t("Filters")}>
      <fieldset>
        <legend>{t("Category")}</legend>
        {categories.map((c) => (
          <label key={c} className="levi-pool-check">
            <input
              type="checkbox"
              checked={filters.categories.includes(c)}
              onChange={() => set({ categories: toggle(filters.categories, c) })}
            />
            <span className="grow">{t(CATEGORY_LABELS[c])}</span>
            <span className="levi-pool-count">
              {(facets?.categories[c] || 0).toLocaleString()}
            </span>
          </label>
        ))}
      </fieldset>
      <fieldset>
        <legend>{t("Task")}</legend>
        <input
          className="levi-input levi-pool-full"
          type="search"
          placeholder={t("Search tasks")}
          aria-label={t("Search tasks")}
          value={filters.search}
          onChange={(e) => set({ search: e.target.value })}
        />
      </fieldset>
      <fieldset>
        <legend>{t("Outcome")}</legend>
        {(
          [
            ["all", "All"],
            ["robot_flag_success", "Robot flag: success"],
            ["verified_success", "Verified success"],
          ] as [OutcomeFilter, string][]
        ).map(([value, label]) => (
          <label key={value} className="levi-pool-check">
            <input
              type="radio"
              name="pool-outcome"
              checked={filters.outcome === value}
              onChange={() => set({ outcome: value })}
            />
            <span className="grow">{t(label)}</span>
            {value !== "all" && (
              <span className="levi-pool-count">
                {(facets?.outcomes[value] || 0).toLocaleString()}
              </span>
            )}
          </label>
        ))}
      </fieldset>
      <fieldset>
        <legend>{t("Source")}</legend>
        <input
          className="levi-input levi-pool-full"
          type="search"
          placeholder={t("Filter sources")}
          aria-label={t("Filter sources")}
          value={sourceSearch}
          onChange={(e) => setSourceSearch(e.target.value)}
        />
        <div className="levi-pool-scroll">
          {shownSources.map((s) => (
            <label key={s.source} className="levi-pool-check" title={s.path}>
              <input
                type="checkbox"
                checked={filters.sources.includes(s.source)}
                onChange={() =>
                  set({ sources: toggle(filters.sources, s.source) })
                }
              />
              <span className="grow levi-pool-ellipsis">
                {shortSource(s.source)}
              </span>
              <span className="levi-pool-count">
                {s.episodes.toLocaleString()}
              </span>
            </label>
          ))}
        </div>
        {sources.length > 12 && (
          <button
            type="button"
            className="levi-pool-link"
            onClick={() => setAllSources(!allSources)}
          >
            {allSources
              ? t("Show fewer")
              : `${t("Show all")} (${sources.length})`}
          </button>
        )}
      </fieldset>
      {policies.length > 0 && (
        <fieldset>
          <legend>{t("Policy")}</legend>
          <div className="levi-pool-scroll">
            {policies.map(([policy, n]) => (
              <label key={policy} className="levi-pool-check" title={policy}>
                <input
                  type="checkbox"
                  checked={filters.policies.includes(policy)}
                  onChange={() =>
                    set({ policies: toggle(filters.policies, policy) })
                  }
                />
                <span className="grow levi-pool-ellipsis">{policy}</span>
                <span className="levi-pool-count">{n.toLocaleString()}</span>
              </label>
            ))}
          </div>
        </fieldset>
      )}
      <fieldset>
        <legend>{t("Date")}</legend>
        <div className="levi-pool-dates">
          <label>
            <span>{t("From")}</span>
            <input
              className="levi-input"
              type="date"
              value={filters.dateFrom}
              min={facets?.date_min || undefined}
              max={facets?.date_max || undefined}
              onChange={(e) => set({ dateFrom: e.target.value })}
            />
          </label>
          <label>
            <span>{t("To")}</span>
            <input
              className="levi-input"
              type="date"
              value={filters.dateTo}
              min={facets?.date_min || undefined}
              max={facets?.date_max || undefined}
              onChange={(e) => set({ dateTo: e.target.value })}
            />
          </label>
        </div>
      </fieldset>
      <fieldset>
        <legend>{t("Show hidden")}</legend>
        <label className="levi-pool-check">
          <input
            type="checkbox"
            checked={filters.showHeldout}
            onChange={() => set({ showHeldout: !filters.showHeldout })}
          />
          <span className="grow">{t("Held-out test set")}</span>
          {!filters.showHeldout && (
            <span className="levi-pool-count">
              {(facets?.hidden_heldout || 0).toLocaleString()}
            </span>
          )}
        </label>
        <label className="levi-pool-check">
          <input
            type="checkbox"
            checked={filters.showCopies}
            onChange={() => set({ showCopies: !filters.showCopies })}
          />
          <span className="grow">{t("Copies")}</span>
          {!filters.showCopies && (
            <span className="levi-pool-count">
              {(facets?.hidden_copies || 0).toLocaleString()}
            </span>
          )}
        </label>
        <label className="levi-pool-check">
          <input
            type="checkbox"
            checked={filters.showArchive}
            onChange={() => set({ showArchive: !filters.showArchive })}
          />
          <span className="grow">{t("Archive")}</span>
          {!filters.showArchive && (
            <span className="levi-pool-count">
              {(facets?.archive || 0).toLocaleString()}
            </span>
          )}
        </label>
      </fieldset>
      <button
        type="button"
        className="levi-secondary"
        onClick={() => onChange(EMPTY_FILTERS)}
      >
        {t("Clear filters")}
      </button>
    </aside>
  );
}
