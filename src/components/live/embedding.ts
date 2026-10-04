// The live page is shown by the product LEVI (it finds the live workspace,
// levi/live/locate.py) and, when the service is started with `--ui`, by the
// live workspace's own LEVI. What differs between the two, decided here so
// it can be tested without a browser.
import type { DatasetDetail, LiveDisabledReason } from "./types";

/** Headline and explanation (catalog keys) when there is no live service to
 * show. */
export function disabledText(reason: LiveDisabledReason | undefined): {
  title: string;
  body: string;
} {
  if (reason === "product_workspace")
    return {
      title: "The live annotation service is set up wrongly",
      body: "What names the live workspace (LEVI_LIVE_WORKSPACE or the service's status file) names a product LEVI's own workspace. The live service needs a workspace of its own: start it with `levi live start --workspace <folder>`.",
    };
  if (reason === "not_live")
    return {
      title: "The live annotation workspace was not found",
      body: "The folder named as the live workspace (LEVI_LIVE_WORKSPACE or the service's status file) does not exist or is not a live workspace. Start the service with `levi live start`; this page finds it by itself.",
    };
  return {
    title: "The live annotation service is not set up",
    body: "Nothing here has run `levi live start` yet. Once the service has started, this page shows its evaluations and labelling; it keeps its own workspace, and nothing of it is written into this LEVI's workspace.",
  };
}

export interface LiveLink {
  href: string;
  /** On another LEVI (the live workspace's own page): opens in a new tab. */
  external: boolean;
}

/** Where the dataset's viewer and its review (Conversion & review) are. In
 * the live workspace's own LEVI: right here. In the product LEVI: on the
 * live workspace's own page, when the service runs one; else nowhere (the
 * product LEVI does not hold the live datasets). */
export function datasetLinks(detail: DatasetDetail | null | undefined): {
  viewer: LiveLink | null;
  review: LiveLink | null;
} {
  if (!detail) return { viewer: null, review: null };
  if (!detail.embedded) {
    return {
      viewer: detail.repo_id
        ? { href: `/${detail.repo_id}`, external: false }
        : null,
      review: { href: "/workbench", external: false },
    };
  }
  const page = (detail.live_ui ?? "").replace(/\/+$/, "");
  if (!page) return { viewer: null, review: null };
  return {
    viewer: detail.repo_id
      ? { href: `${page}/${detail.repo_id}`, external: true }
      : null,
    review: { href: `${page}/workbench`, external: true },
  };
}

/** This LEVI is the live workspace's own (`levi live start --ui`). */
export function isLiveWorkspace(
  enabled: boolean | null,
  embedded: boolean | null,
): boolean {
  return enabled === true && embedded === false;
}

/** The training pool is the product LEVI's: the live workspace's own LEVI
 * does not offer one, so there is only one. Not offered either while that
 * is not known yet (no link that appears and then goes). */
export function offersTrainingPool(
  enabled: boolean | null,
  embedded: boolean | null,
): boolean {
  return enabled !== null && !isLiveWorkspace(enabled, embedded);
}
