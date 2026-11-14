"use client";
// Small pieces shared by the run list, the launch panel and the run window:
// the mode labels, the 15-state graph, a note box and the error line.
import {
  Ban,
  CircleCheck,
  CircleDot,
  Circle,
  FlaskConical,
  Hand,
  OctagonX,
  type LucideIcon,
} from "lucide-react";
import type { ReactNode } from "react";
import { Badge, Icon } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import {
  EXECUTION_MODE_LABEL,
  movesRobot,
  RESET_MODE_LABEL,
  STATE_LABEL,
  stateGraph,
} from "./run-logic";
import type { ExecutionMode, ResetMode } from "./types";

/** One look for "does this move the robot": a mode (or a plan) that moves it is
 * `warning` with the warning icon; one that cannot (dry run, shadow) is
 * `neutral` with a flask. Used everywhere an execution mode or a motion flag
 * is drawn, so the same word never has two colours. `moves` wins over
 * `execution` when both are given. */
export function ExecutionBadge({
  execution,
  moves,
  children,
}: {
  execution?: ExecutionMode | string | null;
  moves?: boolean;
  children?: ReactNode;
}) {
  const { t } = useLocale();
  const real = moves ?? movesRobot(execution ?? undefined);
  const label =
    children ??
    t(
      EXECUTION_MODE_LABEL[execution as ExecutionMode] ??
        "automatic.run.mode.unknown",
    );
  return (
    <Badge
      tone={real ? "warning" : "neutral"}
      icon={real ? undefined : FlaskConical}
    >
      {label}
    </Badge>
  );
}

/** The two labels of a run: how it executes and how the scene is reset. */
export function ModeChips({
  execution,
  reset,
}: {
  execution?: ExecutionMode | null;
  reset?: ResetMode | null;
}) {
  const { t } = useLocale();
  return (
    <span className="ar-chips">
      {execution && <ExecutionBadge execution={execution} />}
      {reset && (
        <Badge tone="neutral" icon={null}>
          {t(RESET_MODE_LABEL[reset] ?? "automatic.run.reset.unknown")}
        </Badge>
      )}
    </span>
  );
}

export function stateName(state: string, t: (text: string) => string): string {
  return t(STATE_LABEL[state] ?? "automatic.run.state.unknown").replace(
    "{state}",
    state,
  );
}

const NODE_ICON: Record<string, LucideIcon> = {
  WAIT_HUMAN: Hand,
  FAULT_LOCKED: OctagonX,
  COMPLETED: CircleCheck,
};

/** The state machine as three lanes. The current state has a thick border, a
 * filled dot and the words "now"; a state the run cannot reach has a dashed
 * border, a "ban" icon and its reason in words. */
export function StateGraph({
  state,
  resetMode,
}: {
  state: string;
  resetMode?: ResetMode | null;
}) {
  const { t } = useLocale();
  const lanes = stateGraph(state, resetMode);
  return (
    <div
      className="ar-graph"
      role="group"
      aria-label={t("automatic.run.graph")}
    >
      {lanes.map((lane) => (
        <div className="ar-lane" key={lane.id}>
          <h3>{t(lane.key)}</h3>
          <ol>
            {lane.nodes.map((node) => {
              const glyph =
                node.status === "current"
                  ? CircleDot
                  : node.status === "disabled"
                    ? Ban
                    : (NODE_ICON[node.state] ?? Circle);
              const tone =
                node.state === "FAULT_LOCKED"
                  ? "danger"
                  : node.state === "WAIT_HUMAN"
                    ? "warning"
                    : "";
              return (
                <li
                  key={node.state}
                  className={`ar-node ar-node--${node.status} ${
                    tone ? `ar-node--${tone}` : ""
                  }`}
                  aria-current={node.status === "current" ? "step" : undefined}
                  data-state={node.state}
                  data-status={node.status}
                >
                  <span className="ar-node__name">
                    <Icon icon={glyph} />
                    {stateName(node.state, t)}
                  </span>
                  {node.status === "current" && (
                    <span className="ar-node__note">
                      {t("automatic.run.graph.now")}
                    </span>
                  )}
                  {node.status === "disabled" && (
                    <span className="ar-node__note">
                      {t("automatic.run.graph.manual_reset")}
                    </span>
                  )}
                </li>
              );
            })}
          </ol>
        </div>
      ))}
    </div>
  );
}

export function Note({
  tone,
  title,
  icon,
  children,
  role,
  id,
}: {
  tone?: "warning" | "danger" | "info";
  title?: ReactNode;
  icon?: LucideIcon;
  children?: ReactNode;
  role?: "status" | "alert";
  /** For a control that names this note as its description. */
  id?: string;
}) {
  return (
    <section
      className={`ar-note ${tone ? `ar-note--${tone}` : ""}`}
      role={role}
      id={id}
    >
      {title && (
        <strong>
          {icon && <Icon icon={icon} />}
          {title}
        </strong>
      )}
      {children}
    </section>
  );
}
