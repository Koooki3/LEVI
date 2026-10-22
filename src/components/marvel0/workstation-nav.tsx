"use client";
import { useLocale } from "@/components/levi-locale";

const SECTIONS = ["arm", "policy", "client", "run", "autonomy"] as const;
const LABELS = {
  arm: "marvel0.nav.arm",
  policy: "marvel0.nav.policy",
  client: "marvel0.nav.client",
  run: "marvel0.nav.run",
  autonomy: "marvel0.nav.autonomy",
};

/** Navigation only: moving between steps never starts their services. */
export function WorkstationNav() {
  const { t } = useLocale();
  return (
    <nav
      className="rg-workstation-nav"
      aria-label={t("marvel0.nav.label")}
      data-testid="marvel0-step-nav"
    >
      {SECTIONS.map((id, i) => (
        <a key={id} href={`#marvel0-step-${id}`} className="ds-focus">
          <span aria-hidden="true">{i + 1}</span>
          {t(LABELS[id])}
        </a>
      ))}
    </nav>
  );
}
