"use client";
// marvel0. The status cards, one per component of the real-robot stack
// and its support services, come from LEVI's two read-only status routes: the
// backend reads files, the kernel's socket table and process lists and runs
// two read-only queries (nvidia-smi, systemctl --user list-units); nothing in
// them connects to a robot port, sends a command or starts or stops anything.
// The action section at the end (marvel0-actions.tsx) has two cards that say
// exactly what they do: one starts and stops the policy server unit this page
// created, the other writes the demo client's command for a person to copy.
// Nothing on the page is an emergency stop or a safety interlock
// (docs/SETUP_WIZARD.md, "Status probes").
import "@/components/pages-ui/pages.css";
import "@/components/marvel0/marvel0-styles.css";
import { useMemo } from "react";
import { Note } from "@/components/pages-ui/feedback";
import { useLocale } from "@/components/levi-locale";
import {
  Freshness,
  IdleBannerView,
  Marvel0CardView,
} from "@/components/marvel0/marvel0-card";
import { Marvel0Actions } from "@/components/marvel0/marvel0-actions";
import { marvel0View } from "@/components/marvel0/marvel0-logic";
import { sayText } from "@/components/marvel0/marvel0-say";
import { useMarvel0Poll } from "@/components/marvel0/use-marvel0-poll";

export default function Marvel0Page() {
  const { t } = useLocale();
  const poll = useMarvel0Poll();
  const view = useMemo(
    () =>
      marvel0View({
        setup: poll.setup,
        marvel0: poll.marvel0,
        setupError: poll.setupError,
        marvel0Error: poll.marvel0Error,
      }),
    [poll.setup, poll.marvel0, poll.setupError, poll.marvel0Error],
  );
  return (
    <main className="ds-root pg-workbench rg-page">
      <h1>{t("marvel0")}</h1>
      <Note tone="warning">
        <strong>{t("marvel0.scope.title")}</strong>
        <ul className="rg-scope">
          <li>{t("marvel0.scope.read_only")}</li>
          <li>{t("marvel0.scope.no_connect")}</li>
          <li>{t("marvel0.scope.actions")}</li>
          <li>{t("marvel0.scope.no_estop")}</li>
        </ul>
      </Note>
      <Freshness
        loaded={poll.loaded}
        lastOk={poll.lastOk}
        delay={poll.delay}
        failures={poll.failures}
        errors={[poll.setupError, poll.marvel0Error].filter(Boolean)}
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
              aria-labelledby={`marvel0-group-${group.id}`}
            >
              <h2 id={`marvel0-group-${group.id}`}>{t(group.title)}</h2>
              <div className="rg-grid">
                {group.cards.map((card) => (
                  <Marvel0CardView key={card.id} card={card} />
                ))}
              </div>
            </section>
          ))}
          <Marvel0Actions />
        </>
      )}
    </main>
  );
}
