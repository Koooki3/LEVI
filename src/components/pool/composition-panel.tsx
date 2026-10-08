"use client";
import { useColon } from "@/components/pages-ui/messages";
import { useState } from "react";
import {
  ArrowDown,
  ArrowUp,
  Eye,
  FilePlus,
  ListPlus,
  Lock,
  RotateCcw,
  Save,
  Trash2,
  X,
} from "lucide-react";
import { Button, Icon, IconButton, Skeleton, Tooltip } from "@/components/ds";
import { EmptyLine, RequestProblem } from "@/components/pages-ui/feedback";
// Motion comes with it; the composition is only on the training pool page.
import { ReorderList } from "@/components/ds/ReorderList";
import { useLocale } from "@/components/levi-locale";
import { useConfirmAction } from "@/components/shell/confirm";
import { GripperMix, MixSummary, PickedList, TaskPick } from "./task-pick";
import {
  CATEGORY_LABELS,
  METHOD_LABELS,
  OUTCOME_SOURCE_LABELS,
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

/** Server warnings; a blocking one stops the export until fixed. */
export function PoolWarnings({ warnings }: { warnings: PoolWarning[] }) {
  const { t, language } = useLocale();
  if (!warnings?.length) return null;
  return (
    <div role="status">
      <ul className="pg-pool-warnings">
        {warnings.map((w) => (
          <li
            key={w.code}
            className={stopsExport(w) ? "blocking" : ""}
            data-level={w.level}
          >
            <strong>{stopsExport(w) ? t("Blocks export") : t("Note")}</strong>{" "}
            {warningText(w, t, language)}
            {w.tasks?.length ? ` (${w.tasks.slice(0, 3).join("; ")})` : ""}
            {typeof w.episodes === "number" &&
            w.episodes &&
            !TIMING_WARNING_CODES.has(w.code)
              ? ` (${w.episodes.toLocaleString()})`
              : ""}
            {w.ids?.length ? ` (${w.ids.length})` : ""}
          </li>
        ))}
      </ul>
    </div>
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
  const colon = useColon();
  const confirm = useConfirmAction();
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
    ...recipe.sources.map((s) => `${t("Source")}${colon}${s}`),
    ...recipe.policies.map((p) => `${t("Policy")}${colon}${p}`),
    ...(recipe.policy_models || []).map(
      (p) => `${t("Policy model")}${colon}${p}`,
    ),
    ...(recipe.policy_checkpoints || []).map(
      (p) => `${t("Policy checkpoint")}${colon}${p}`,
    ),
    ...(recipe.policy_methods || []).map(
      (p) => `${t("How it was run")}${colon}${t(METHOD_LABELS[p] || p)}`,
    ),
    ...(recipe.grippers || []).map(
      (g) => `${t("Gripper")}${colon}${gripperLabel(g, t)}`,
    ),
    ...(recipe.robots || []).map((r) => `${t("Robot")}${colon}${r}`),
    ...(recipe.outcome !== "all"
      ? [
          t(
            recipe.outcome === "robot_flag_success"
              ? "Robot flag: success"
              : recipe.outcome === "human_verified_success"
                ? "Human-labelled success"
                : recipe.outcome === "checked_success"
                  ? "Human-labelled or verified success"
                  : "Verified success",
          ),
        ]
      : []),
    ...(recipe.date_from ? [`≥ ${recipe.date_from}`] : []),
    ...(recipe.date_to ? [`≤ ${recipe.date_to}`] : []),
  ];
  return (
    <section className="pg-pool-card" aria-labelledby="pool-composition">
      <h2 id="pool-composition">{t("Composition")}</h2>
      <p className="pg-pool-hint">
        {t(
          "Tasks are exported in this order. Drag a task, or use its arrow buttons.",
        )}
      </p>
      {recipe.tasks.length === 0 ? (
        <EmptyLine icon={ListPlus}>
          {t("Add tasks from the task table.")}
        </EmptyLine>
      ) : (
        <ReorderList
          className="pg-pool-order"
          label={t("Task order")}
          items={recipe.tasks}
          getKey={(entry) => entry.task}
          getLabel={(entry) => entry.task}
          onReorder={(tasks) => set({ tasks })}
          renderItem={(entry, index) => {
            const task = entry.task;
            const counts = perTask.get(task);
            return (
              <>
                <div className="pg-pool-row">
                  <span className="pg-pool-rank tabular">{index + 1}</span>
                  <Tooltip content={task}>
                    <span className="grow pg-pool-ellipsis">{task}</span>
                  </Tooltip>
                  <span className="pg-pool-count tabular">
                    {counts
                      ? counts.episodes.toLocaleString()
                      : preview
                        ? "0"
                        : "…"}{" "}
                    {t("episodes")}
                  </span>
                  <IconButton
                    size="sm"
                    icon={ArrowUp}
                    label={`${t("Move up")}${colon}${task}`}
                    disabled={index === 0}
                    onClick={() => reorder(index, index - 1)}
                  />
                  <IconButton
                    size="sm"
                    icon={ArrowDown}
                    label={`${t("Move down")}${colon}${task}`}
                    disabled={index === recipe.tasks.length - 1}
                    onClick={() => reorder(index, index + 1)}
                  />
                  <IconButton
                    size="sm"
                    icon={X}
                    label={`${t("Remove")}${colon}${task}`}
                    onClick={() =>
                      set({
                        tasks: recipe.tasks.filter((x) => x.task !== task),
                      })
                    }
                  />
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
              </>
            );
          }}
        />
      )}
      <p className="sr-only" aria-live="polite">
        {announce}
      </p>
      <div className="pg-pool-fields">
        <label>
          <span
            title={t("Used for tasks that have no episode count of their own")}
          >
            {t("Per-task cap")}
          </span>
          <input
            className="ds-input ds-focus"
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
            className="ds-input ds-focus"
            type="number"
            value={recipe.seed}
            onChange={(e) =>
              set({ seed: Math.floor(Number(e.target.value) || 0) })
            }
          />
        </label>
      </div>
      <label className="pg-pool-check">
        <input
          type="checkbox"
          checked={recipe.include_nonstandard}
          onChange={() =>
            set({ include_nonstandard: !recipe.include_nonstandard })
          }
        />
        <span>{t("Include non-standard folders")}</span>
      </label>
      {(recipe.include_holdback ||
        preview?.excluded_holdback ||
        preview?.holdback?.included) && (
        <label className="pg-pool-check">
          <input
            type="checkbox"
            checked={!!recipe.include_holdback}
            onChange={() => set({ include_holdback: !recipe.include_holdback })}
          />
          <span>{t("Include held-back episodes")}</span>
        </label>
      )}
      <label className="pg-pool-check">
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
        <label className="pg-pool-check">
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
      <div className="pg-pool-constraints">
        <span className="pg-pool-label">{t("Constraints")}</span>
        {constraints.length ? (
          constraints.map((c) => (
            <span key={c} className="pg-pool-chip">
              {c}
            </span>
          ))
        ) : (
          <span className="pg-pool-muted">
            {t("None: every category and source")}
          </span>
        )}
        <p className="pg-pool-hint">
          {t(
            "Category, source, outcome, policy and date come from the filters on the left.",
          )}
        </p>
        {recipe.exclude.length > 0 && (
          <p className="pg-pool-hint">
            {recipe.exclude.length} {t("episodes excluded by hand")} ·{" "}
            <Button
              size="sm"
              variant="ghost"
              icon={RotateCcw}
              onClick={() => set({ exclude: [] })}
            >
              {t("Reset")}
            </Button>
          </p>
        )}
      </div>

      <div
        className="pg-pool-preview"
        aria-live="polite"
        aria-busy={previewing}
      >
        <h3>{t("Preview")}</h3>
        {previewError ? (
          <RequestProblem
            action="The preview could not be computed"
            message={previewError}
          />
        ) : !preview ? (
          recipe.tasks.length ? (
            <div className="pg-pool-stats">
              <span className="sr-only" role="status">
                {t("Computing…")}
              </span>
              <Skeleton height={56} radius="sm" />
              <Skeleton height={56} radius="sm" />
              <Skeleton height={56} radius="sm" />
            </div>
          ) : (
            <EmptyLine icon={Eye}>
              {t("Choose tasks to see a preview.")}
            </EmptyLine>
          )
        ) : (
          <>
            <div className="pg-pool-stats">
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
                  <Icon icon={Lock} />
                  {preview.excluded_heldout.toLocaleString()}
                </strong>
                <span>{t("Held-out excluded")}</span>
              </div>
            </div>
            {excludedReasons.length > 0 && (
              <table className="pg-pool-reasons">
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
              <p className="pg-pool-hint">
                {t("Outcome from")}
                {colon}
                {Object.entries(preview.outcome_sources)
                  .map(
                    ([source, n]) =>
                      `${t(OUTCOME_SOURCE_LABELS[source] || source)} ${n.toLocaleString()}`,
                  )
                  .join(" · ")}
              </p>
            )}
            {preview.mix && <MixSummary mix={preview.mix} />}
            <GripperMix
              grippers={preview.grippers}
              declared={preview.declared?.gripper}
            />
            <PoolWarnings warnings={preview.warnings} />
            {preview.tasks_without_episodes.length > 0 && (
              <p className="pg-warnings">
                {t("No episodes left for")}
                {colon}
                {preview.tasks_without_episodes.join(", ")}
              </p>
            )}
          </>
        )}
      </div>

      <div className="pg-pool-recipes">
        <h3>{t("Saved recipes")}</h3>
        <div className="pg-row">
          <label className="grow pg-pool-stack">
            <span>{t("Recipe name")}</span>
            <input
              className="ds-input ds-focus"
              value={recipe.name}
              pattern="[A-Za-z0-9][A-Za-z0-9._\-]{0,99}"
              title={t("Letters, digits, dot, dash and underscore")}
              onChange={(e) => set({ name: e.target.value })}
            />
          </label>
          <Button icon={Save} disabled={!recipe.name} onClick={onSave}>
            {t("Save")}
          </Button>
        </div>
        <div className="pg-row">
          <select
            className="ds-input ds-focus grow"
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
          <Button disabled={!chosenRecipe} onClick={() => onLoad(chosenRecipe)}>
            {t("Load")}
          </Button>
          <Button
            variant="ghost"
            className="pg-danger-text"
            icon={Trash2}
            disabled={!chosenRecipe}
            onClick={async () => {
              const name = chosenRecipe;
              if (
                await confirm({
                  title: `${t("Delete recipe")} ${name}?`,
                  confirmLabel: t("Delete"),
                  tone: "danger",
                })
              ) {
                onDelete(name);
                setChosenRecipe("");
              }
            }}
          >
            {t("Delete")}
          </Button>
        </div>
        <Button
          size="sm"
          variant="ghost"
          icon={FilePlus}
          className="pg-align-start"
          onClick={onClear}
        >
          {t("Start a new composition")}
        </Button>
      </div>
    </section>
  );
}
