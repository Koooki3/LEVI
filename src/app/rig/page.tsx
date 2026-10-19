"use client";
// Rig console: one card per component of the real-robot stack and its
// support services, from LEVI's two read-only status routes. The backend
// reads files, the kernel's socket table and process lists and runs two
// read-only queries (nvidia-smi, systemctl --user list-units); nothing here
// connects to a robot port, sends a command or starts or stops anything. It
// is not an emergency stop and not a safety interlock (docs/SETUP_WIZARD.md,
// "Status probes").
import "@/components/pages-ui/pages.css";
import "@/components/rig/rig-styles.css";
import { useMemo } from "react";
import { Note } from "@/components/pages-ui/feedback";
import { useLocale } from "@/components/levi-locale";
import {
  Freshness,
  IdleBannerView,
  RigCardView,
} from "@/components/rig/rig-card";
import { rigView } from "@/components/rig/rig-logic";
import { sayText } from "@/components/rig/rig-say";
import { useRigPoll } from "@/components/rig/use-rig-poll";

export default function RigPage() {
  const { t } = useLocale();
  const poll = useRigPoll();
  const view = useMemo(
    () =>
      rigView({
        setup: poll.setup,
        rig: poll.rig,
        setupError: poll.setupError,
        rigError: poll.rigError,
      }),
    [poll.setup, poll.rig, poll.setupError, poll.rigError],
  );
  return (
    <main className="ds-root pg-workbench rg-page">
      <h1>{t("Rig console")}</h1>
      <Note tone="warning">
        <strong>{t("rig.scope.title")}</strong>
        <ul className="rg-scope">
          <li>{t("rig.scope.read_only")}</li>
          <li>{t("rig.scope.no_connect")}</li>
          <li>{t("rig.scope.no_estop")}</li>
        </ul>
      </Note>
      <Freshness
        loaded={poll.loaded}
        lastOk={poll.lastOk}
        delay={poll.delay}
        failures={poll.failures}
        errors={[poll.setupError, poll.rigError].filter(Boolean)}
        onRefresh={poll.refresh}
      />
      {!poll.loaded ? (
        <p role="status" aria-busy="true">
          {t("Loading…")}
        </p>
      ) : (
        <>
          {view.notes.map((note, index) => (
            <Note key={index} tone="warning" role="status">
              {sayText(note, t)}
            </Note>
          ))}
          <IdleBannerView banner={view.banner} />
          {view.groups.map((group) => (
            <section
              key={group.id}
              className="rg-section"
              aria-labelledby={`rig-group-${group.id}`}
            >
              <h2 id={`rig-group-${group.id}`}>{t(group.title)}</h2>
              <div className="rg-grid">
                {group.cards.map((card) => (
                  <RigCardView key={card.id} card={card} />
                ))}
              </div>
            </section>
          ))}
        </>
      )}
    </main>
  );
}
