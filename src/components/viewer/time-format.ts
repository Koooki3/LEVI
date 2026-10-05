/** Seconds as m:ss (the playback bar and the timeline ruler). */
export function formatClock(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds));
  const minutes = Math.floor(whole / 60);
  return `${minutes}:${String(whole % 60).padStart(2, "0")}`;
}

/** Seconds as m:ss.cc, for positions that need hundredths (timeline). */
export function formatClockPrecise(seconds: number): string {
  const hundredths = Math.max(0, Math.round(seconds * 100));
  const minutes = Math.floor(hundredths / 6000);
  const rest = (hundredths % 6000) / 100;
  return `${minutes}:${rest.toFixed(2).padStart(5, "0")}`;
}

/** A number of seconds or a rate for display: at most two decimals, no
 * trailing zeros (9.349721999915758 -> "9.35", 3 -> "3"). */
export function roundTo2(value: number): string {
  if (!Number.isFinite(value)) return "";
  return String(Math.round(value * 100) / 100);
}
