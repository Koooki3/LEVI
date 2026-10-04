/**
 * Whether the development-only design specimen (/design) is served:
 * always under `next dev`, in production only with `LEVI_DESIGN_PAGE=1`.
 */
export function designPageEnabled(
  env: Record<string, string | undefined> = process.env,
): boolean {
  return env.NODE_ENV !== "production" || env.LEVI_DESIGN_PAGE === "1";
}
