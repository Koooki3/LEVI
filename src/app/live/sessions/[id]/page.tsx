"use client";
import "@/components/pages-ui/pages.css";
import { useParams } from "next/navigation";
import { SessionDetail } from "@/components/live/session-detail";
import { parseSessionIdentity } from "@/components/live/session-route";
export default function LiveSessionPage() {
  const params = useParams<{ id: string }>();
  // The parser accepts both Next route representations without double decoding.
  const raw = Array.isArray(params?.id) ? params.id[0] : params?.id;
  return <SessionDetail key={raw} identity={parseSessionIdentity(raw ?? "")} />;
}
