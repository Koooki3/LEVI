"use client";
// /automatic/new: set up and start an automatic evaluation from the page.
import "@/components/pages-ui/pages.css";
import "@/components/automatic/wizard-styles.css";
import { useRouter } from "next/navigation";
import { WizardPage } from "@/components/automatic/wizard-page";

export default function NewEvaluationPage() {
  const router = useRouter();
  return (
    <main className="ds-root pg-workbench aw-page">
      <WizardPage navigate={(href) => router.push(href)} />
    </main>
  );
}
