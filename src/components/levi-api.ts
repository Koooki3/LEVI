const reviewRevisions = new Map<string, string>();
export async function leviApi<T>(path: string, body?: unknown): Promise<T> {
  const repo = path.startsWith("review?")
    ? new URLSearchParams(path.split("?")[1]).get("repo_id")
    : body && typeof body === "object" && "repo_id" in body
      ? String(body.repo_id)
      : null;
  const revision = repo ? reviewRevisions.get(repo) : undefined;
  const response = await fetch(
    `/api/levi/${path}`,
    body === undefined
      ? { cache: "no-store" }
      : {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            ...(revision ? { "x-levi-annotation-revision": revision } : {}),
          },
          body: JSON.stringify(body),
        },
  );
  if (!response.ok) {
    const data = await response
      .json()
      .catch(() => ({ detail: `HTTP ${response.status}` }));
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : JSON.stringify(data.detail),
    );
  }
  const observed = response.headers.get("x-levi-annotation-revision");
  if (repo && observed) reviewRevisions.set(repo, observed);
  return response.json();
}
/**
 * File name for an exported review or analysis document:
 * `<dataset>-<kind>-<YYYYmmddTHH>.json`, in UTC like LEVI's run ids.
 * Named after the dataset it describes, stamped to the hour, never a hash.
 */
export function exportName(repoId: string, kind: string, at = new Date()) {
  const dataset = (repoId.split("/").pop() || "dataset").replace(
    /[^A-Za-z0-9._-]/g,
    "_",
  );
  const stamp = at.toISOString().slice(0, 13).replace(/-/g, "");
  return `${dataset}-${kind}-${stamp}.json`;
}
export function downloadJson(value: unknown, name: string) {
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(value, null, 2)], { type: "application/json" }),
  );
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
