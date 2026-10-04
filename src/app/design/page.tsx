import type { Metadata } from "next";
import { notFound } from "next/navigation";
import "@/styles/tokens.css";
import "@/styles/ds.css";
import "./design.css";
import Specimen from "./specimen";
import { designPageEnabled } from "@/lib/design/gate";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "LEVI · Design specimen",
  robots: { index: false, follow: false },
};

/**
 * The design-system specimen (stage 0 of the UI redesign): tokens and
 * components in light and dark side by side. Development only: in production
 * src/middleware.ts answers 404 before this page (and its metadata and
 * styles) is reached, unless `LEVI_DESIGN_PAGE=1`. Not linked from the
 * navigation.
 */
export default async function DesignPage({
  searchParams,
}: {
  searchParams: Promise<{ only?: string; motion?: string }>;
}) {
  if (!designPageEnabled()) notFound();
  const params = await searchParams;
  const only =
    params.only === "light" || params.only === "dark" ? params.only : null;
  return <Specimen only={only} reduceMotion={params.motion === "reduce"} />;
}
