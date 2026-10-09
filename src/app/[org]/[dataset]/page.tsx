import { redirect } from "next/navigation";
import { Suspense } from "react";
import { LocalDatasetEntry } from "@/components/viewer/local-dataset-entry";

export default async function DatasetRootPage({
  params,
}: {
  params: Promise<{ org: string; dataset: string }>;
}) {
  const { org, dataset } = await params;
  if (org === "local")
    return (
      <Suspense>
        <LocalDatasetEntry dataset={dataset} />
      </Suspense>
    );
  const episodeN =
    process.env.EPISODES?.split(/\s+/)
      .map((x) => parseInt(x.trim(), 10))
      .filter((x) => !isNaN(x))[0] ?? 0;

  redirect(`/${org}/${dataset}/episode_${episodeN}`);
}
