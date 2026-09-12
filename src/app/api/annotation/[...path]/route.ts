import { NextRequest } from "next/server";
import { backendProxy } from "@/utils/backendProxy";
export const runtime = "nodejs";
export const dynamic = "force-dynamic";
function handle(
  request: NextRequest,
  context: { params: Promise<{ path: string[] }> },
) {
  return backendProxy(request, context, true);
}
export const GET = handle;
export const POST = handle;
export const DELETE = handle;
