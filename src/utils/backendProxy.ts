import { NextRequest } from "next/server";
import { uiToken } from "./serviceToken";

/** Same-origin bridge. Runtime configuration also works after a production build. */
export async function backendProxy(
  request: NextRequest,
  context: { params: Promise<{ path: string[] }> },
  annotation = false,
) {
  const origin = request.headers.get("origin");
  if (
    !["GET", "HEAD"].includes(request.method) &&
    origin &&
    new URL(origin).host !== request.headers.get("host")
  ) {
    return Response.json(
      { detail: "Cross-origin writes are disabled" },
      { status: 403 },
    );
  }
  const { path } = await context.params;
  if (path.some((part) => part === "." || part === ".." || part.includes("\\")))
    return new Response("Forbidden", { status: 403 });
  const base = process.env.LEVI_BACKEND_URL || "http://127.0.0.1:7861";
  const target = `${base}/${annotation ? "annotations/api" : "api/levi"}/${path.map(encodeURIComponent).join("/")}${request.nextUrl.search}`;
  const headers = new Headers();
  for (const key of [
    "content-type",
    "range",
    "if-none-match",
    "if-modified-since",
    "cookie",
    "authorization",
    "last-event-id",
  ]) {
    const value = request.headers.get(key);
    if (value) headers.set(key, value);
  }
  const token = uiToken();
  if (token) headers.set("x-levi-ui-token", token);
  const revision = request.headers.get("x-levi-annotation-revision");
  if (revision) headers.set("x-levi-annotation-revision", revision);
  let body: ArrayBuffer | undefined;
  if (!["GET", "HEAD"].includes(request.method)) {
    body = await request.arrayBuffer();
    if (body.byteLength > 16 * 1024 * 1024)
      return Response.json(
        { detail: "Request exceeds 16 MiB" },
        { status: 413 },
      );
  }
  try {
    const upstream = await fetch(target, {
      method: request.method,
      headers,
      body,
      cache: "no-store",
      signal: AbortSignal.any([
        request.signal,
        AbortSignal.timeout(30 * 60 * 1000),
      ]),
    });
    const responseHeaders = new Headers();
    for (const key of [
      "content-type",
      "content-length",
      "content-range",
      "accept-ranges",
      "content-disposition",
      "etag",
      "x-levi-annotation-revision",
      "last-modified",
      // Dataset files change while LEVI runs (levi/sync.py): keep the
      // backend's "revalidate before reuse" policy.
      "cache-control",
    ]) {
      const value = upstream.headers.get(key);
      if (value) responseHeaders.set(key, value);
    }
    return new Response(request.method === "HEAD" ? null : upstream.body, {
      status: upstream.status,
      headers: responseHeaders,
    });
  } catch {
    return Response.json(
      {
        detail:
          "LEVI backend unavailable. Start both services with uv run levi serve.",
      },
      { status: 502 },
    );
  }
}
