/**
 * Language instruction extraction utilities
 * Consolidates duplicated logic from fetch-data.ts
 */

/**
 * Extract language instructions from episode data rows
 * Consolidates logic from lines 232-258 and 573-626 in fetch-data.ts
 *
 * This function checks for language_instruction fields in the provided rows.
 * It supports both single and numbered language instruction fields
 * (language_instruction, language_instruction_2, language_instruction_3, etc.)
 *
 * @param episodeData - Array of episode data rows
 * @param sampleIndices - Indices of rows to check (default: [0] for first row only)
 * @returns Concatenated language instructions or undefined if none found
 */
export function extractLanguageInstructions(
  episodeData: Record<string, unknown>[],
  sampleIndices: number[] = [0],
): string | undefined {
  if (episodeData.length === 0) return undefined;

  for (const idx of sampleIndices) {
    if (idx < 0 || idx >= episodeData.length) continue;
    const row = episodeData[idx];
    const keys = Object.keys(row)
      .filter(
        (key) =>
          key === "language_instruction" ||
          /^language_instruction_\d+$/.test(key),
      )
      .sort((a, b) => {
        const number = (key: string) =>
          key === "language_instruction"
            ? 1
            : Number(key.slice("language_instruction_".length));
        return number(a) - number(b);
      });
    const instructions = keys
      .map((key) => row[key])
      .filter(
        (value): value is string =>
          typeof value === "string" && value.trim().length > 0,
      )
      .map((value) => value.trim());
    if (instructions.length > 0) return instructions.join("\n");
  }

  return undefined;
}

/**
 * Extract task from task_index by looking up in tasks metadata
 * Helper function for task extraction with proper type handling
 *
 * @param taskIndex - Task index (can be BigInt or number)
 * @param tasksData - Array of task metadata objects
 * @returns Task string or undefined if not found
 */
export function extractTaskFromMetadata(
  taskIndex: unknown,
  tasksData: Record<string, unknown>[],
): string | undefined {
  const index =
    typeof taskIndex === "bigint"
      ? Number(taskIndex)
      : typeof taskIndex === "number"
        ? taskIndex
        : typeof taskIndex === "string" && taskIndex.trim().length > 0
          ? Number(taskIndex)
          : Number.NaN;
  if (!Number.isInteger(index) || index < 0) return undefined;

  // The task_index column is authoritative. A dataframe row's physical
  // position is only a compatibility fallback for old files that omit it.
  const indexed = tasksData.find((row) => {
    const value = row.task_index;
    const candidate =
      typeof value === "bigint"
        ? Number(value)
        : typeof value === "number"
          ? value
          : typeof value === "string"
            ? Number(value)
            : Number.NaN;
    return Number.isInteger(candidate) && candidate === index;
  });
  const taskData = indexed ?? tasksData[index];
  if (!taskData) return undefined;

  for (const key of ["task", "__index_level_0__", "name", "instruction"]) {
    const value = taskData[key];
    if (typeof value === "string" && value.trim().length > 0) {
      return value.trim();
    }
  }
  return undefined;
}
