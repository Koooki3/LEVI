// Which 3D view the episode viewer offers for a dataset (design:
// lab/levi-hub/programs/levi2/REPLAY-3D-DESIGN-20261021.md §2.2).
//
// - `panel`: the FR3 replay in the Episodes tab (joints, or end-effector
//   pose only). Decided by `detectFr3Replay`, from robot_type and the columns.
// - `urdf`: the inherited "3D Replay" tab for SO-100/101, OpenArm and Unitree
//   G1, which loads its models from the network.
// - `none`: neither.
//
// The FR3 family is decided first. `isG1Robot` calls any robot_type that
// contains "g1" a Unitree G1, so `franka_g1x` would open the G1 viewer; a
// robot_type that names a Franka/FR3/Panda never goes to the URDF viewer.
// (`hasURDFSupport` itself is unchanged; fixing it is a separate change.)
import { hasURDFSupport } from "@/lib/so101-robot";
import {
  detectFr3Replay,
  fr3RobotFamily,
  type Fr3ReplayDetection,
} from "./fr3-columns";

export type ReplayKind =
  | {
      kind: "panel";
      detection: Fr3ReplayDetection;
      /** `fr3`: robot_type names the FR3 family; `generic`: robot_type empty or generic (pose only). */
      family: "fr3" | "generic";
    }
  | { kind: "urdf" }
  | { kind: "none" };

export function replayKind(
  robotType: string | null | undefined,
  flatKeys: readonly string[],
): ReplayKind {
  const detection = detectFr3Replay({ robot_type: robotType }, flatKeys);
  const family = fr3RobotFamily(robotType);
  if (detection.mode !== "none" && family !== "other") {
    return { kind: "panel", detection, family };
  }
  if (family === "fr3") return { kind: "none" };
  if (hasURDFSupport(robotType ?? null)) return { kind: "urdf" };
  return { kind: "none" };
}

/**
 * The column keys of an episode's `flatChartData`. A row leaves out a column
 * whose value is not a number, so the first row alone can miss one: the union
 * of the first, middle and last rows.
 */
export function flatKeysOf(rows: readonly Record<string, number>[]): string[] {
  if (rows.length === 0) return [];
  const picks = new Set([0, Math.floor(rows.length / 2), rows.length - 1]);
  const keys = new Set<string>();
  for (const at of picks)
    for (const key of Object.keys(rows[at])) keys.add(key);
  return [...keys];
}
