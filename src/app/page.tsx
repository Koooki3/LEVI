// Modified for LEVI (2026); see NOTICE and docs/UPSTREAM.md.
"use client";
/**
 * Home: the work entrance (continue, needs you, running, live evaluation,
 * recent datasets). The introduction that used to be here is on /guide.
 * Older links `/?path=…` and `/?dataset=…&episode=…&t=…` still redirect.
 */
import { Suspense, useEffect } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { HomeDashboard } from "@/components/home/home-dashboard";
import "@/styles/home.css";

export default function Home() {
  return (
    <Suspense>
      <Redirects />
      <HomeDashboard />
    </Suspense>
  );
}

function Redirects() {
  const router = useRouter();
  const params = useSearchParams();
  useEffect(() => {
    const path = params.get("path");
    const dataset = params.get("dataset");
    if (path?.startsWith("/") && !path.startsWith("//")) router.replace(path);
    else if (dataset && /^[\w.-]+\/[\w.-]+$/.test(dataset))
      router.replace(
        `/${dataset}${params.get("episode") ? `/episode_${Number(params.get("episode"))}` : ""}${params.get("t") ? `?t=${Number(params.get("t"))}` : ""}`,
      );
  }, [params, router]);
  return null;
}
