import { NextResponse } from "next/server";
import { designPageEnabled } from "@/lib/design/gate";

/**
 * The design specimen (/design) is a development page. In production it is
 * answered here, before routing, so the 404 carries none of its metadata or
 * styles; `LEVI_DESIGN_PAGE=1` opens it. Runs only for /design.
 */
export function middleware() {
  if (designPageEnabled()) return NextResponse.next();
  return new NextResponse("Not found", {
    status: 404,
    headers: { "content-type": "text/plain; charset=utf-8" },
  });
}

export const config = { matcher: ["/design", "/design/:path*"] };
