"use client";
// The window of one automatic evaluation run (/automatic/runs/<id>).
import "@/components/pages-ui/pages.css";
import "@/components/automatic/run-styles.css";
import { useParams } from "next/navigation";
import { RunWindow } from "@/components/automatic/run-window";

export default function AutomaticRunPage() {
  const params = useParams<{ id: string }>();
  const raw = Array.isArray(params?.id) ? params.id[0] : params?.id;
  return <RunWindow runId={decodeURIComponent(raw ?? "")} />;
}
