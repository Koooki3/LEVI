"use client";
// Small pieces shared by the run list, the launch panel and the run window:
// the mode labels, the 13-state graph, a note box and the error line.
import {
  Ban,
  CircleCheck,
  CircleDot,
  Circle,
  Hand,
  OctagonX,
  type LucideIcon,
} from "lucide-react";
import type { ReactNode } from "react";
import { Badge, Icon } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import {
  EXECUTION_MODE_LABEL,
  RESET_MODE_LABEL,
  STATE_LABEL,
  stateGraph,
} from "./run-logic";
import type { ExecutionMode, ResetMode } from "./types";

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
      {execution && (
        <Badge tone="info" icon={null}>
          {t(EXECUTION_MODE_LABEL[execution] ?? "automatic.run.mode.unknown")}
        </Badge>
      )}
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
}: {
  tone?: "warning" | "danger" | "info";
  title?: ReactNode;
  icon?: LucideIcon;
  children?: ReactNode;
  role?: "status" | "alert";
}) {
  return (
    <section
      className={`ar-note ${tone ? `ar-note--${tone}` : ""}`}
      role={role}
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
