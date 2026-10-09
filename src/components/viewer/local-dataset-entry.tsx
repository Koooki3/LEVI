"use client";

import { useEffect } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useLocale } from "@/components/levi-locale";
import Loading from "@/components/loading-component";
import { useLocalEpisodes } from "./use-local-episodes";

export function LocalDatasetEntry({ dataset }: { dataset: string }) {
  const session = useSearchParams().get("live_session");
  const { indices, loading, error } = useLocalEpisodes(
    "local",
    dataset,
    session,
  );
  const router = useRouter();
  const { t } = useLocale();
  useEffect(() => {
    if (indices?.length)
      router.replace(
        `/local/${encodeURIComponent(dataset)}/episode_${indices[0]}${session ? `?live_session=${encodeURIComponent(session)}` : ""}`,
      );
  }, [indices, dataset, session, router]);
  if (loading || indices?.length) return <Loading />;
  return (
    <div className="vw-root ds-root p-6" role="status">
      <p>{error || t("No episodes remain in this selection.")}</p>
      <Link href="/workbench">{t("Back to datasets")}</Link>
    </div>
  );
}
