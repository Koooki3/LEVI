"use client";
import { Button, Field, Input, Select } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import {
  EMPTY_LIVE_FILTERS,
  liveFiltersActive,
  type LiveFilters,
  type LiveStateFilter,
} from "./live-filters";

const STATES: [LiveStateFilter, string][] = [
  ["", "All statuses"],
  ["active", "In progress"],
  ["waiting", "Waiting"],
  ["finished", "Finished"],
  ["problem", "Fault or error"],
  ["stopped", "Stopped"],
  ["approval", "Awaiting a person's approval"],
  ["idle", "Idle"],
];

export function LiveFilterBar({
  filters,
  options,
  onChange,
}: {
  filters: LiveFilters;
  options: { models: string[]; tasks: string[] };
  onChange: (value: LiveFilters) => void;
}) {
  const { t } = useLocale();
  const update = <K extends keyof LiveFilters>(key: K, value: LiveFilters[K]) =>
    onChange({ ...filters, [key]: value });
  const dated = !!(filters.from || filters.through);
  return (
    <section
      className="pg-live-section pg-live-filters"
      aria-label={t("Filters")}
    >
      <div className="pg-live-toolbar">
        <Field label={t("Keyword")} className="pg-live-toolbar__search">
          <Input
            type="search"
            value={filters.query}
            placeholder={t("Model, task, run or checkpoint")}
            onChange={(e) => update("query", e.target.value)}
          />
        </Field>
        <Field label={t("Status")}>
          <Select
            value={filters.state}
            onChange={(e) => update("state", e.target.value as LiveStateFilter)}
          >
            {STATES.map(([value, label]) => (
              <option key={value} value={value}>
                {t(label)}
              </option>
            ))}
          </Select>
        </Field>
        <Field label={t("Model")}>
          <Select
            value={filters.model}
            onChange={(e) => update("model", e.target.value)}
          >
            <option value="">{t("All models")}</option>
            {options.models.map((model) => (
              <option key={model} value={model}>
                {model}
              </option>
            ))}
          </Select>
        </Field>
        <Field label={t("Task")}>
          <Select
            value={filters.task}
            onChange={(e) => update("task", e.target.value)}
          >
            <option value="">{t("All tasks")}</option>
            {options.tasks.map((task) => (
              <option key={task} value={task}>
                {task}
              </option>
            ))}
          </Select>
        </Field>
        <Button
          size="sm"
          variant="ghost"
          className="pg-live-toolbar__clear"
          disabled={!liveFiltersActive(filters)}
          onClick={() => onChange({ ...EMPTY_LIVE_FILTERS })}
        >
          {t("Clear filters")}
        </Button>
      </div>
      <details className="pg-live-dates" open={dated || undefined}>
        <summary>
          {t("Date of latest activity")}
          {dated && (
            <span className="pg-pool-muted">
              {" · "}
              {filters.from || "…"} – {filters.through || "…"}
            </span>
          )}
        </summary>
        <div className="pg-live-dates__fields">
          <Field label={t("Activity from")}>
            <Input
              type="date"
              value={filters.from}
              max={filters.through || undefined}
              onChange={(e) => update("from", e.target.value)}
            />
          </Field>
          <Field label={t("Activity through")}>
            <Input
              type="date"
              value={filters.through}
              min={filters.from || undefined}
              onChange={(e) => update("through", e.target.value)}
            />
          </Field>
        </div>
      </details>
    </section>
  );
}
