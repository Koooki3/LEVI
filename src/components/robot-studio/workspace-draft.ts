/** Preview-only point boundary in fr3_link0 (meters), never a motion approval. */
export type WorkspaceBox = {
  min: [number, number, number];
  max: [number, number, number];
};
export type WorkspaceFields = [string, string, string, string, string, string];
// Matches the pose editor's input envelope; this is not a hardware safety limit.
export const WORKSPACE_PREVIEW_LIMIT_M = 2;

function finitePoint(value: unknown): value is [number, number, number] {
  return (
    Array.isArray(value) &&
    value.length === 3 &&
    Array.from(value).every(Number.isFinite)
  );
}

export function isValidWorkspaceBox(value: unknown): value is WorkspaceBox {
  if (!value || typeof value !== "object") return false;
  const box = value as Partial<WorkspaceBox>;
  if (!finitePoint(box.min) || !finitePoint(box.max)) return false;
  return box.min.every(
    (minimum, i) =>
      minimum >= -WORKSPACE_PREVIEW_LIMIT_M &&
      box.max![i] <= WORKSPACE_PREVIEW_LIMIT_M &&
      minimum < box.max![i],
  );
}

export function cloneWorkspaceBox(
  value: WorkspaceBox | null,
): WorkspaceBox | null {
  if (value === null) return null;
  if (!isValidWorkspaceBox(value))
    throw new RangeError("Invalid preview workspace box");
  return { min: [...value.min], max: [...value.max] };
}

/** Six millimeter strings ordered minX,minY,minZ,maxX,maxY,maxZ.
 * All blank means unset. A partially completed or invalid box throws; callers
 * must never turn invalid input into a silently unset boundary.
 */
export function parseWorkspaceFields(
  fields: WorkspaceFields,
): WorkspaceBox | null {
  if (
    !Array.isArray(fields) ||
    fields.length !== 6 ||
    !Array.from(fields).every((field) => typeof field === "string")
  ) {
    throw new RangeError("Expected six workspace field strings");
  }
  const trimmed = fields.map((field) => field.trim());
  if (trimmed.every((field) => field === "")) return null;
  const decimal = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/;
  if (trimmed.some((field) => !decimal.test(field)))
    throw new RangeError("Workspace coordinates must all be numeric");
  const meters = trimmed.map((field) => Number(field) / 1000);
  const box: WorkspaceBox = {
    min: [meters[0], meters[1], meters[2]],
    max: [meters[3], meters[4], meters[5]],
  };
  if (!isValidWorkspaceBox(box))
    throw new RangeError(
      "Workspace min must be below max within the preview input envelope",
    );
  return box;
}

/** Full precision is retained: rounding a narrow draft could invert its bounds. */
export function workspaceFields(box: WorkspaceBox | null): WorkspaceFields {
  if (box === null) return ["", "", "", "", "", ""];
  if (!isValidWorkspaceBox(box))
    throw new RangeError("Invalid preview workspace box");
  return [...box.min, ...box.max].map((value) =>
    String(value * 1000),
  ) as WorkspaceFields;
}

/** Inclusive faces. null means UNSET, never unrestricted or approved.
 * Invalid points and invalid non-null boxes throw even when the box is unset.
 * Only the target point is checked, not tool sweep, obstacles or robot links.
 */
export function pointInWorkspace(
  box: WorkspaceBox | null,
  point: [number, number, number],
): boolean | null {
  if (!finitePoint(point))
    throw new RangeError("Expected a finite target point");
  if (box === null) return null;
  if (!isValidWorkspaceBox(box))
    throw new RangeError("Invalid preview workspace box");
  return point.every(
    (coordinate, i) => coordinate >= box.min[i] && coordinate <= box.max[i],
  );
}
