"use client";
import { useState } from "react";
import { useLocale } from "@/components/levi-locale";
import { GripperMix, MixSummary, PickedList, TaskPick } from "./task-pick";
import {
  CATEGORY_LABELS,
  METHOD_LABELS,
  REASON_LABELS,
  gripperLabel,
  TIMING_WARNING_CODES,
  stopsExport,
  warningText,
  type PickedEpisodes,
  type PoolWarning,
  type Preview,
  type Recipe,
  type TaskEntry,
} from "./types";

const OUTCOME_SOURCES: Record<string, string> = {
  human: "human label",
  robot_flag: "robot flag",
  sft_demonstration: "demonstration",
  none: "none",
};

/** Server warnings; a blocking one stops the export until fixed. */
export function PoolWarnings({ warnings }: { warnings: PoolWarning[] }) {
  const { t } = useLocale();
  if (!warnings?.length) return null;
  return (
    <ul className="levi-pool-warnings" role="status">
      {warnings.map((w) => (
        <li
          key={w.code}
          className={stopsExport(w) ? "blocking" : ""}
          data-level={w.level}
        >
          <strong>{stopsExport(w) ? t("Blocks export") : t("Note")}</strong>{" "}
          {warningText(w, t)}
          {w.tasks?.length ? ` (${w.tasks.slice(0, 3).join("; ")})` : ""}
          {w.episodes && !TIMING_WARNING_CODES.has(w.code)
            ? ` (${w.episodes.toLocaleString()})`
            : ""}
          {w.ids?.length ? ` (${w.ids.length})` : ""}
        </li>
      ))}
    </ul>
  );
}

function move<T>(list: T[], from: number, to: number): T[] {
  if (to < 0 || to >= list.length || from === to) return list;
  const next = [...list];
  const [item] = next.splice(from, 1);
  next.splice(to, 0, item);
  return next;
}

/** Right column: the ordered task list (drag by the grip, or the up / down
 * buttons) with, per task, how many episodes it contributes (editable, with
 * the picked episodes on request), the default cap, seed, the filters it
 * carries, the live preview with its mix, and the saved recipes. */
export function CompositionPanel({
  recipe,
  preview,
  previewError,
  previewing,
  recipes,
  onChange,
  onSave,
  onLoad,
  onDelete,
  onClear,
  onListEpisodes,
  refreshKey,
}: {
  recipe: Recipe;
  preview: Preview | null;
  previewError: string;
  previewing: boolean;
  recipes: Recipe[];
  onChange: (next: Recipe) => void;
  onSave: () => void;
  onLoad: (name: string) => void;
  onDelete: (name: string) => void;
  onClear: () => void;
  onListEpisodes: (task: string) => Promise<PickedEpisodes>;
  refreshKey: string;
}) {
  const { t } = useLocale();
  const [dragging, setDragging] = useState<number | null>(null);
  const [over, setOver] = useState<number | null>(null);
  const [announce, setAnnounce] = useState("");
  const [chosenRecipe, setChosenRecipe] = useState("");
  const set = (patch: Partial<Recipe>) => onChange({ ...recipe, ...patch });
  const reorder = (from: number, to: number) => {
    if (to < 0 || to >= recipe.tasks.length) return;
    set({ tasks: move(recipe.tasks, from, to) });
    setAnnounce(`${recipe.tasks[from].task} → ${to + 1}`);
  };
  const patch = (task: string, change: Partial<TaskEntry>) =>
    set({
      tasks: recipe.tasks.map((e) =>
        e.task === task ? { ...e, ...change } : e,
      ),
    });
  const perTask = new Map((preview?.tasks || []).map((p) => [p.task, p]));
  const excludedReasons = Object.entries(preview?.excluded || {}).filter(
    ([, n]) => n > 0,
  );
  const constraints: string[] = [
    ...recipe.categories.map((c) => t(CATEGORY_LABELS[c] || c)),
    ...recipe.sources.map((s) => `${t("Source")}: ${s}`),
    ...recipe.policies.map((p) => `${t("Policy")}: ${p}`),
    ...(recipe.policy_models || []).map((p) => `${t("Policy model")}: ${p}`),
    ...(recipe.policy_checkpoints || []).map(
      (p) => `${t("Policy checkpoint")}: ${p}`,
    ),
    ...(recipe.policy_methods || []).map(
      (p) => `${t("How it was run")}: ${t(METHOD_LABELS[p] || p)}`,
    ),
    ...(recipe.grippers || []).map(
      (g) => `${t("Gripper")}: ${gripperLabel(g, t)}`,
    ),
    ...(recipe.robots || []).map((r) => `${t("Robot")}: ${r}`),
    ...(recipe.outcome !== "all"
      ? [
          t(
            recipe.outcome === "robot_flag_success"
              ? "Robot flag: success"
              : recipe.outcome === "human_verified_success"
                ? "Human-labelled success"
                : "Verified success",
          ),
        ]
      : []),
    ...(recipe.date_from ? [`≥ ${recipe.date_from}`] : []),
    ...(recipe.date_to ? [`≤ ${recipe.date_to}`] : []),
  ];
  return (
    <section className="levi-pool-card" aria-labelledby="pool-composition">
      <h2 id="pool-composition">{t("Composition")}</h2>
      <p className="levi-pool-hint">
        {t(
          "Tasks are exported in this order. Drag a task, or use its arrow buttons.",
        )}
      </p>
      <ol className="levi-pool-order" aria-label={t("Task order")}>
        {recipe.tasks.map((entry, index) => {
          const task = entry.task;
          const counts = perTask.get(task);
          return (
            <li
              key={task}
              className={
                over === index && dragging !== null && dragging !== index
                  ? "drop"
                  : dragging === index
                    ? "dragging"
                    : ""
              }
              onDragOver={(e) => {
                if (dragging === null) return;
                e.preventDefault();
                setOver(index);
              }}
              onDragLeave={() => setOver(null)}
              onDrop={(e) => {
                e.preventDefault();
                if (dragging !== null) reorder(dragging, index);
                setDragging(null);
                setOver(null);
              }}
            >
              <div
                className="levi-pool-row"
                draggable
                onDragStart={(e) => {
                  setDragging(index);
                  e.dataTransfer.effectAllowed = "move";
                  e.dataTransfer.setData("text/plain", String(index));
                }}
                onDragEnd={() => {
                  setDragging(null);
                  setOver(null);
                }}
              >
                <span className="levi-pool-grip" aria-hidden>
                  ⋮⋮
                </span>
                <span className="levi-pool-rank tabular">{index + 1}</span>
                <span className="grow levi-pool-ellipsis" title={task}>
                  {task}
                </span>
                <span className="levi-pool-count tabular">
                  {counts
                    ? counts.episodes.toLocaleString()
                    : preview
                      ? "0"
                      : "…"}
                </span>
                <button
                  type="button"
                  aria-label={`${t("Move up")}: ${task}`}
                  disabled={index === 0}
                  onClick={() => reorder(index, index - 1)}
                >
                  ↑
                </button>
                <button
                  type="button"
                  aria-label={`${t("Move down")}: ${task}`}
                  disabled={index === recipe.tasks.length - 1}
                  onClick={() => reorder(index, index + 1)}
                >
                  ↓
                </button>
                <button
                  type="button"
                  aria-label={`${t("Remove")}: ${task}`}
                  onClick={() =>
                    set({ tasks: recipe.tasks.filter((x) => x.task !== task) })
                  }
                >
                  ×
                </button>
              </div>
              <TaskPick
                entry={entry}
                report={counts}
                onChange={(change) => patch(task, change)}
              />
              <PickedList
                task={task}
                refreshKey={refreshKey}
                load={onListEpisodes}
              />
            </li>
          );
        })}
        {recipe.tasks.length === 0 && (
          <li className="levi-pool-empty">
            {t("Add tasks from the task table.")}
          </li>
        )}
      </ol>
      <p className="sr-only" aria-live="polite">
        {announce}
      </p>
      <div className="levi-pool-fields">
        <label>
          <span
            title={t("Used for tasks that have no episode count of their own")}
          >
            {t("Per-task cap")}
          </span>
          <input
            className="levi-input"
            type="number"
            min={1}
            placeholder={t("No cap")}
            value={recipe.per_task_cap ?? ""}
            onChange={(e) =>
              set({
                per_task_cap: e.target.value
                  ? Math.max(1, Math.floor(Number(e.target.value)))
                  : null,
              })
            }
          />
        </label>
        <label>
          <span>{t("Seed")}</span>
          <input
            className="levi-input"
            type="number"
            value={recipe.seed}
            onChange={(e) =>
              set({ seed: Math.floor(Number(e.target.value) || 0) })
            }
          />
        </label>
      </div>
      <label className="levi-pool-check">
        <input
          type="checkbox"
          checked={recipe.include_nonstandard}
          onChange={() =>
            set({ include_nonstandard: !recipe.include_nonstandard })
          }
        />
        <span>{t("Include non-standard folders")}</span>
      </label>
      <label className="levi-pool-check">
        <input
          type="checkbox"
          checked={!!recipe.allow_unlinked_sources}
          onChange={() =>
            set({ allow_unlinked_sources: !recipe.allow_unlinked_sources })
          }
        />
        <span>
          {t("Allow raw captures and unlinked LeRobot data of one task")}
        </span>
      </label>
      {(recipe.allow_mixed_gripper ||
        preview?.warnings?.some((w) => w.code === "mixed_gripper")) && (
        <label className="levi-pool-check">
          <input
            type="checkbox"
            checked={!!recipe.allow_mixed_gripper}
            onChange={() =>
              set({ allow_mixed_gripper: !recipe.allow_mixed_gripper })
            }
          />
          <span>{t("Allow mixing grippers in one export")}</span>
        </label>
      )}
      <div className="levi-pool-constraints">
        <span className="levi-pool-label">{t("Constraints")}</span>
        {constraints.length ? (
          constraints.map((c) => (
            <span key={c} className="levi-pool-chip">
              {c}
            </span>
          ))
        ) : (
          <span className="levi-pool-muted">
            {t("None: every category and source")}
          </span>
        )}
        <p className="levi-pool-hint">
          {t(
            "Category, source, outcome, policy and date come from the filters on the left.",
          )}
        </p>
        {recipe.exclude.length > 0 && (
          <p className="levi-pool-hint">
            {recipe.exclude.length} {t("episodes excluded by hand")} ·{" "}
            <button
              type="button"
              className="levi-pool-link"
              onClick={() => set({ exclude: [] })}
            >
              {t("Reset")}
            </button>
          </p>
        )}
      </div>

      <div
        className="levi-pool-preview"
        aria-live="polite"
        aria-busy={previewing}
      >
        <h3>{t("Preview")}</h3>
        {previewError ? (
          <p className="levi-error">{previewError}</p>
        ) : !preview ? (
          <p className="levi-pool-muted">
            {recipe.tasks.length
              ? t("Computing…")
              : t("Choose tasks to see a preview.")}
          </p>
        ) : (
          <>
            <div className="levi-pool-stats">
              <div>
                <strong className="tabular">
                  {preview.episodes.toLocaleString()}
                </strong>
                <span>{t("Episodes")}</span>
              </div>
              <div>
                <strong className="tabular">
                  {preview.frames.toLocaleString()}
                </strong>
                <span>{t("Frames")}</span>
              </div>
              <div className="heldout">
                <strong className="tabular">
                  {preview.excluded_heldout.toLocaleString()}
                </strong>
                <span>{t("Held-out excluded")}</span>
              </div>
            </div>
            {excludedReasons.length > 0 && (
              <table className="levi-pool-reasons">
                <caption>{t("Left out, by reason")}</caption>
                <tbody>
                  {excludedReasons.map(([reason, n]) => (
                    <tr key={reason}>
                      <th scope="row">{t(REASON_LABELS[reason] || reason)}</th>
                      <td className="num tabular">{n.toLocaleString()}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {Object.keys(preview.outcome_sources || {}).length > 0 && (
              <p className="levi-pool-hint">
                {t("Outcome from")}:{" "}
                {Object.entries(preview.outcome_sources)
                  .map(
                    ([source, n]) =>
                      `${t(OUTCOME_SOURCES[source] || source)} ${n.toLocaleString()}`,
                  )
                  .join(" · ")}
              </p>
            )}
            {preview.mix && <MixSummary mix={preview.mix} />}
            <GripperMix grippers={preview.grippers} />
            <PoolWarnings warnings={preview.warnings} />
            {preview.tasks_without_episodes.length > 0 && (
              <p className="levi-warnings">
                {t("No episodes left for")}:{" "}
                {preview.tasks_without_episodes.join(", ")}
              </p>
            )}
          </>
        )}
      </div>

      <div className="levi-pool-recipes">
        <h3>{t("Saved recipes")}</h3>
        <div className="levi-row">
          <label className="grow levi-pool-stack">
            <span>{t("Recipe name")}</span>
            <input
              className="levi-input"
              value={recipe.name}
              pattern="[A-Za-z0-9][A-Za-z0-9._\-]{0,99}"
              title={t("Letters, digits, dot, dash and underscore")}
              onChange={(e) => set({ name: e.target.value })}
            />
          </label>
          <button
            type="button"
            className="levi-primary"
            disabled={!recipe.name}
            onClick={onSave}
          >
            {t("Save")}
          </button>
        </div>
        <div className="levi-row">
          <select
            className="levi-input grow"
            aria-label={t("Saved recipes")}
            value={chosenRecipe}
            onChange={(e) => setChosenRecipe(e.target.value)}
          >
            <option value="">{t("Choose a saved recipe")}</option>
            {recipes.map((r) => (
              <option key={r.name} value={r.name}>
                {r.name} · {r.tasks.length} {t("Tasks").toLowerCase()}
              </option>
            ))}
          </select>
          <button
            type="button"
            className="levi-secondary"
            disabled={!chosenRecipe}
            onClick={() => onLoad(chosenRecipe)}
          >
            {t("Load")}
          </button>
          <button
            type="button"
            className="levi-secondary danger"
            disabled={!chosenRecipe}
            onClick={() => {
              if (window.confirm(`${t("Delete recipe")} ${chosenRecipe}?`)) {
                onDelete(chosenRecipe);
                setChosenRecipe("");
              }
            }}
          >
            {t("Delete")}
          </button>
        </div>
        <button type="button" className="levi-pool-link" onClick={onClear}>
          {t("Start a new composition")}
        </button>
      </div>
    </section>
  );
}
