"use client";
import { Check } from "lucide-react";
import { Icon } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";

export type PoolStep = 1 | 2 | 3;

/** Where the person is: no task chosen yet (1 · Select), tasks in the
 * composition (2 · Compose), an export started or shown (3 · Export). */
export function poolStep(tasks: number, exporting: boolean): PoolStep {
  if (exporting) return 3;
  return tasks > 0 ? 2 : 1;
}

const STEPS: { step: PoolStep; label: string; target: string }[] = [
  { step: 1, label: "Select", target: "pool-tasks" },
  { step: 2, label: "Compose", target: "pool-composition" },
  { step: 3, label: "Export", target: "pool-export" },
];

/** Bring a section into view and move focus to its heading. */
export function goToSection(id: string, focusId?: string) {
  const heading = document.getElementById(id);
  if (!heading) return;
  heading.closest("section")?.scrollIntoView({ block: "start" });
  const target = (focusId && document.getElementById(focusId)) || heading;
  if (target === heading && !heading.hasAttribute("tabindex"))
    heading.setAttribute("tabindex", "-1");
  (target as HTMLElement).focus({ preventScroll: true });
}

/** The step bar: ① Select ② Compose ③ Export. The current step is bold
 * (`aria-current="step"`), earlier ones carry a check; each jumps to its
 * section. */
export function PoolSteps({ current }: { current: PoolStep }) {
  const { t } = useLocale();
  return (
    <nav className="pg-steps-bar" aria-label={t("Export steps")}>
      <ol>
        {STEPS.map(({ step, label, target }) => (
          <li key={step}>
            <button
              type="button"
              className="pg-step ds-focus"
              data-state={
                step === current ? "current" : step < current ? "done" : "todo"
              }
              aria-current={step === current ? "step" : undefined}
              onClick={() => goToSection(target)}
            >
              <span className="pg-step__mark" aria-hidden="true">
                {step < current ? <Icon icon={Check} /> : step}
              </span>
              <span>{t(label)}</span>
            </button>
          </li>
        ))}
      </ol>
    </nav>
  );
}
