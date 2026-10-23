/** Keep the explicit scheduler; old API responses only carried RTC. */
export type InferenceMode = "plain" | "rtc" | "vlash";
export function inferenceMode(
  value: unknown,
  rtc: boolean | null | undefined,
): InferenceMode {
  return value === "plain" || value === "rtc" || value === "vlash"
    ? value
    : rtc === true
      ? "rtc"
      : "plain";
}
export const modeLabel = (value: unknown, rtc?: boolean | null): string =>
  `marvel0.inference.${inferenceMode(value, rtc)}`;
export function validOverlap(value: string | undefined): boolean {
  const typed = value?.trim() || "2";
  return (
    /^\d+$/.test(typed) &&
    Number.isSafeInteger(Number(typed)) &&
    Number(typed) > 0
  );
}
