"use client";
import { FilterX } from "lucide-react";
import { Button, Tooltip } from "@/components/ds";
import { useState } from "react";
import { useLocale } from "@/components/levi-locale";
import {
  CATEGORIES,
  CATEGORY_LABELS,
  METHOD_LABELS,
  POLICY_METHODS,
  gripperLabel,
  type Facets,
  type HoldbackFilter,
  type OutcomeFilter,
} from "./types";

export interface Filters {
  categories: string[];
  sources: string[];
  search: string;
  outcome: OutcomeFilter;
  holdback: HoldbackFilter; // episodes on a hold-back list: all, only, none
  policies: string[]; // the old single field (a saved recipe's checkpoints)
  policyModels: string[];
  policyCheckpoints: string[];
  policyMethods: string[];
  grippers: string[];
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
  holdback: "all",
  policies: [],
  policyModels: [],
  policyCheckpoints: [],
  policyMethods: [],
  grippers: [],
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

/** One policy facet: a checkbox per value with its episode count. */
function PolicyFacet({
  legend,
  entries,
  selected,
  label,
  onToggle,
}: {
  legend: string;
  entries: [string, number][];
  selected: string[];
  label?: (value: string) => string;
  onToggle: (value: string) => void;
}) {
  const { t } = useLocale();
  return (
    <fieldset>
      <legend>{t(legend)}</legend>
      <div className="pg-pool-scroll">
        {entries.map(([value, n]) => (
          <Tooltip key={value} content={value}>
            <label className="pg-pool-check">
              <input
                type="checkbox"
                checked={selected.includes(value)}
                onChange={() => onToggle(value)}
              />
              <span className="grow pg-pool-ellipsis">
                {label ? label(value) : value}
              </span>
              <span className="pg-pool-count">{n.toLocaleString()}</span>
            </label>
          </Tooltip>
        ))}
      </div>
    </fieldset>
  );
}

/** Left column: category, source, task search, outcome, policy (model,
 * checkpoint, how it was run) and date facets with counts; held-out, copies and archive are hidden by default. */
/** Recordings a person removed on the live page: never listed, counted or
 * exported, so the number says why the page shows fewer. Nothing when none. */
export function RemovedInLive({ count }: { count?: number }) {
  const { t } = useLocale();
  if (!count) return null;
  return (
    <div className="pg-pool-removed">
      <p className="pg-pool-check">
        <span className="grow">{t("Episodes removed on the live page")}</span>
        <span className="pg-pool-count">{count.toLocaleString()}</span>
      </p>
      <p className="pg-pool-why">
        {t(
          "Removed by a person on the live page: not listed, counted or exported. They can be restored there.",
        )}
      </p>
    </div>
  );
}

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
  const byCount = (counts: Record<string, number> | undefined) =>
    Object.entries(counts || {}).sort(
      (a, b) => b[1] - a[1] || a[0].localeCompare(b[0]),
    );
  const models = byCount(facets?.policy_models);
  const checkpoints = byCount(facets?.policy_checkpoints);
  const methods = POLICY_METHODS.filter((m) => facets?.policy_methods?.[m]).map(
    (m) => [m, facets?.policy_methods?.[m] || 0] as [string, number],
  );
  const grippers = byCount(facets?.grippers);
  const categories = CATEGORIES.filter(
    (c) => c !== "archive" || filters.showArchive,
  );
  return (
    <aside className="pg-pool-facets" aria-label={t("Filters")}>
      <fieldset>
        <legend>{t("Category")}</legend>
        {categories.map((c) => (
          <label key={c} className="pg-pool-check">
            <input
              type="checkbox"
              checked={filters.categories.includes(c)}
              onChange={() =>
                set({ categories: toggle(filters.categories, c) })
              }
            />
            <span className="grow">{t(CATEGORY_LABELS[c])}</span>
            <span className="pg-pool-count">
              {(facets?.categories[c] || 0).toLocaleString()}
            </span>
          </label>
        ))}
      </fieldset>
      <fieldset>
        <legend>{t("Task")}</legend>
        <input
          className="ds-input ds-focus pg-pool-full"
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
            ["checked_success", "Human-labelled or verified success"],
            ["human_verified_success", "Human-labelled success"],
          ] as [OutcomeFilter, string][]
        ).map(([value, label]) => (
          <label key={value} className="pg-pool-check">
            <input
              type="radio"
              name="pool-outcome"
              checked={filters.outcome === value}
              onChange={() => set({ outcome: value })}
            />
            <span className="grow">{t(label)}</span>
            {value !== "all" && (
              <span className="pg-pool-count">
                {(facets?.outcomes[value] || 0).toLocaleString()}
              </span>
            )}
          </label>
        ))}
      </fieldset>
      {(!!facets?.holdback || filters.holdback !== "all") && (
        <fieldset>
          <legend>{t("Held back")}</legend>
          {(
            [
              ["all", "All"],
              ["only", "Only held back"],
              ["hide", "Without held back"],
            ] as [HoldbackFilter, string][]
          ).map(([value, label]) => (
            <label key={value} className="pg-pool-check">
              <input
                type="radio"
                name="pool-holdback"
                checked={filters.holdback === value}
                onChange={() => set({ holdback: value })}
              />
              <span className="grow">{t(label)}</span>
              {value === "only" && (
                <span className="pg-pool-count">
                  {(facets?.holdback || 0).toLocaleString()}
                </span>
              )}
            </label>
          ))}
          <p className="pg-pool-why">
            {t(
              "Set aside for now: listed here, but a recipe leaves them out unless it includes held-back episodes.",
            )}
          </p>
        </fieldset>
      )}
      <fieldset>
        <legend>{t("Source")}</legend>
        <input
          className="ds-input ds-focus pg-pool-full"
          type="search"
          placeholder={t("Filter sources")}
          aria-label={t("Filter sources")}
          value={sourceSearch}
          onChange={(e) => setSourceSearch(e.target.value)}
        />
        <div className="pg-pool-scroll">
          {shownSources.map((s) => (
            <Tooltip key={s.source} content={s.path}>
              <label className="pg-pool-check">
                <input
                  type="checkbox"
                  checked={filters.sources.includes(s.source)}
                  onChange={() =>
                    set({ sources: toggle(filters.sources, s.source) })
                  }
                />
                <span className="grow pg-pool-ellipsis">
                  {shortSource(s.source)}
                </span>
                <span className="pg-pool-count">
                  {s.episodes.toLocaleString()}
                </span>
              </label>
            </Tooltip>
          ))}
        </div>
        {sources.length > 12 && (
          <Button
            size="sm"
            variant="ghost"
            className="pg-align-start"
            onClick={() => setAllSources(!allSources)}
          >
            {allSources
              ? t("Show fewer")
              : `${t("Show all")} (${sources.length})`}
          </Button>
        )}
      </fieldset>
      {models.length > 0 && (
        <PolicyFacet
          legend="Policy model"
          entries={models}
          selected={filters.policyModels}
          onToggle={(v) =>
            set({ policyModels: toggle(filters.policyModels, v) })
          }
        />
      )}
      {checkpoints.length > 0 && (
        <PolicyFacet
          legend="Policy checkpoint"
          entries={checkpoints}
          selected={filters.policyCheckpoints}
          onToggle={(v) =>
            set({ policyCheckpoints: toggle(filters.policyCheckpoints, v) })
          }
        />
      )}
      {methods.length > 0 && (
        <PolicyFacet
          legend="How it was run"
          entries={methods}
          selected={filters.policyMethods}
          label={(v) => t(METHOD_LABELS[v] || v)}
          onToggle={(v) =>
            set({ policyMethods: toggle(filters.policyMethods, v) })
          }
        />
      )}
      {grippers.length > 0 && (
        <PolicyFacet
          legend="Gripper"
          entries={grippers}
          selected={filters.grippers}
          label={(v) => gripperLabel(v, t)}
          onToggle={(v) => set({ grippers: toggle(filters.grippers, v) })}
        />
      )}
      <fieldset>
        <legend>{t("Date")}</legend>
        <div className="pg-pool-dates">
          <label>
            <span>{t("From")}</span>
            <input
              className="ds-input ds-focus"
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
              className="ds-input ds-focus"
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
        <label className="pg-pool-check">
          <input
            type="checkbox"
            checked={filters.showHeldout}
            onChange={() => set({ showHeldout: !filters.showHeldout })}
          />
          <span className="grow">{t("Held-out test set")}</span>
          {!filters.showHeldout && (
            <span className="pg-pool-count">
              {(facets?.hidden_heldout || 0).toLocaleString()}
            </span>
          )}
        </label>
        <label className="pg-pool-check">
          <input
            type="checkbox"
            checked={filters.showCopies}
            onChange={() => set({ showCopies: !filters.showCopies })}
          />
          <span className="grow">{t("Copies")}</span>
          {!filters.showCopies && (
            <span className="pg-pool-count">
              {(facets?.hidden_copies || 0).toLocaleString()}
            </span>
          )}
        </label>
        <label className="pg-pool-check">
          <input
            type="checkbox"
            checked={filters.showArchive}
            onChange={() => set({ showArchive: !filters.showArchive })}
          />
          <span className="grow">{t("Archive")}</span>
          {!filters.showArchive && (
            <span className="pg-pool-count">
              {(facets?.archive || 0).toLocaleString()}
            </span>
          )}
        </label>
        <RemovedInLive count={facets?.removed_in_live} />
      </fieldset>
      <Button icon={FilterX} onClick={() => onChange(EMPTY_FILTERS)}>
        {t("Clear filters")}
      </Button>
    </aside>
  );
}
