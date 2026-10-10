import { NextRequest } from "next/server";
import { uiToken } from "./serviceToken";

/*
 * Trust boundary of the bridge below.
 *
 * The bridge adds the operator's UI token to every request it forwards, so
 * whoever reaches it acts as the person at the keyboard. It therefore answers
 * only requests that are addressed to this page by a name the operator uses
 * (the Host check: a DNS-rebinding page arrives under its own name), and it
 * takes a write only from the LEVI page itself (the same-origin check: another
 * site, another local web app or a bare script sends no matching Origin).
 * A process running as the same user can still forge both headers or read
 * the key file; the operating-system account stays the real boundary.
 * Scripts write through the `levi` CLI or a scoped agent token instead
 * (docs/API.md, "Trust boundary of the web bridge").
 */

const LOOPBACK = ["127.0.0.1", "localhost", "[::1]"];
const DEFAULT_PORT = "7860";
const HOST_SYNTAX =
  /^(\[[0-9a-f:.]+\]|[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?)(?::(\d{1,5}))?$/i;
const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);
// The one cookie the core reads (the Hugging Face session, levi/service.py).
const FORWARDED_COOKIE = "hf_access_token";

export type HostPort = { name: string; port: string };
export type AllowedHosts = { exact: Set<string>; anyPort: Set<string> };

function validPort(value: string): string | null {
  const port = Number(value);
  return Number.isInteger(port) && port >= 1 && port <= 65535
    ? String(port)
    : null;
}

/** A `Host`-style value (`name[:port]`) in canonical form, or null.
 *
 * Names are lower-cased and IP literals normalised by the URL parser
 * (`[0:0::1]` is `[::1]`); anything with user info, a path, a trailing dot
 * or characters outside a host name is refused. `port` is "" when absent.
 */
export function parseHost(value: string | null | undefined): HostPort | null {
  if (!value) return null;
  const match = HOST_SYNTAX.exec(value);
  if (!match) return null;
  let url: URL;
  try {
    url = new URL(`http://${value}`);
  } catch {
    return null;
  }
  if (url.username || url.password || url.pathname !== "/") return null;
  let port = "";
  if (match[2] !== undefined) {
    const checked = validPort(match[2]);
    if (!checked) return null;
    port = checked;
  }
  return { name: url.hostname, port };
}

function urlHost(value: string | undefined): HostPort | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
    return {
      name: url.hostname,
      port: url.port || (url.protocol === "https:" ? "443" : "80"),
    };
  } catch {
    return null;
  }
}

/** The names this page answers to.
 *
 * Loopback (`127.0.0.1`, `localhost`, `[::1]`) on the page's own port (`PORT`,
 * which `next start` sets, and the port of `LEVI_FRONTEND_URL`, which the
 * launcher sets; 7860 when neither is known), the launcher's own address,
 * a Hugging Face Space's `SPACE_HOST`, and `LEVI_UI_ALLOWED_HOSTS`: names
 * separated by commas or spaces, `name:port` for one port, a bare name for
 * any port. Malformed entries are ignored.
 */
export function allowedHosts(
  env: Record<string, string | undefined> = process.env,
): AllowedHosts {
  const exact = new Set<string>();
  const anyPort = new Set<string>();
  const frontend = urlHost(env.LEVI_FRONTEND_URL);
  const ports = new Set<string>();
  const ownPort = env.PORT ? validPort(env.PORT) : null;
  if (ownPort) ports.add(ownPort);
  if (frontend) ports.add(frontend.port);
  if (!ports.size) ports.add(DEFAULT_PORT);
  for (const name of LOOPBACK)
    for (const port of ports) exact.add(`${name}:${port}`);
  if (frontend) exact.add(`${frontend.name}:${frontend.port}`);
  const space = parseHost(env.SPACE_HOST);
  if (space && !space.port) anyPort.add(space.name);
  for (const entry of (env.LEVI_UI_ALLOWED_HOSTS || "").split(/[\s,]+/)) {
    const host = parseHost(entry);
    if (!host) continue;
    if (host.port) exact.add(`${host.name}:${host.port}`);
    else anyPort.add(host.name);
  }
  return { exact, anyPort };
}

function permitted(host: HostPort, allowed: AllowedHosts): boolean {
  return (
    allowed.anyPort.has(host.name) ||
    allowed.exact.has(`${host.name}:${host.port || "80"}`)
  );
}

/** Whether the request's `Host` names this page. */
export function hostAllowed(
  host: string | null,
  allowed: AllowedHosts = allowedHosts(),
): boolean {
  const parsed = parseHost(host);
  return parsed !== null && permitted(parsed, allowed);
}

/** Whether a write comes from the LEVI page itself.
 *
 * A browser marks its own same-origin requests with `Sec-Fetch-Site:
 * same-origin` and sends `Origin` on every write. Any other fetch site
 * (`cross-site`, `same-site`, `none`) is refused, an `Origin` must be one of
 * the page's own names, and a request with neither header (a script) is
 * refused.
 */
export function writeAllowed(
  headers: Headers,
  allowed: AllowedHosts = allowedHosts(),
): boolean {
  const site = headers.get("sec-fetch-site");
  if (site !== null && site.trim().toLowerCase() !== "same-origin")
    return false;
  const origin = headers.get("origin");
  if (origin !== null) {
    const parsed = urlHost(origin);
    return parsed !== null && permitted(parsed, allowed);
  }
  return site !== null;
}

/** The core's cookie alone, from the browser's `Cookie` header. */
export function forwardedCookie(header: string | null): string | null {
  if (!header) return null;
  for (const part of header.split(";")) {
    const pair = part.trim();
    if (pair.startsWith(`${FORWARDED_COOKIE}=`)) return pair;
  }
  return null;
}

/** Same-origin bridge. Runtime configuration also works after a production build. */
export async function backendProxy(
  request: NextRequest,
  context: { params: Promise<{ path: string[] }> },
  annotation = false,
) {
  const allowed = allowedHosts();
  if (!hostAllowed(request.headers.get("host"), allowed)) {
    return Response.json(
      {
        detail:
          "This LEVI page does not answer to that host name. Open it as http://127.0.0.1:<port>, or add the name to LEVI_UI_ALLOWED_HOSTS.",
      },
      { status: 421 },
    );
  }
  if (
    !SAFE_METHODS.has(request.method) &&
    !writeAllowed(request.headers, allowed)
  ) {
    return Response.json(
      {
        detail:
          "Writes through the web UI must come from the LEVI page itself. Scripts use the levi CLI or a scoped agent token.",
      },
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
    "last-event-id",
  ]) {
    const value = request.headers.get(key);
    if (value) headers.set(key, value);
  }
  // Nothing the browser or a caller sends can stand in for the token below:
  // no Authorization, no X-Forwarded-*, no other cookie. The core reads only
  // the Hugging Face session cookie (for private Hub datasets).
  const cookie = forwardedCookie(request.headers.get("cookie"));
  if (cookie) headers.set("cookie", cookie);
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
      // Report assets (levi/report.py): an SVG must stay sandboxed.
      "content-security-policy",
      "x-content-type-options",
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
