"use client";
// marvel_workstation. The page reads top to bottom as the stack is brought up: the chain
// at the top (M → Desk → A → B → C → policy server → evaluation run, each with
// its state and, for the first one that is not ready, the reason), then step 1
// the arm stack (a card per terminal with its own Start and Stop, the gripper
// test, the guided start and the marker), step 2 the policy server, step 3 the
// task and its command, step 4 the run, and last the services on this machine
// (folded unless one of them is red).
//
// What it reads: LEVI's read-only status routes (status files, units, the
// kernel's socket table; nothing in them connects to a robot port), the arm
// stack (every 2 s) and the hosted client (every second); polling stops while
// the tab is hidden. What it does is done by buttons that say what they do and
// ask for a confirmation: start and stop the parts of the stack, run the
// gripper test, deploy the policy server, start and drive the client. The
// checks behind the buttons stop mistakes, not another program of the same
// account; Franka Desk and the physical emergency stop are the barrier, and
// nothing on the page is an emergency stop (docs/SETUP_WIZARD.md).
//
// Under the title, apart from the steps, a card leads to the offline FR3 3D
// preview (/robot-studio): a model and a solver in the browser, no robot
// connection.
import "@/components/pages-ui/pages.css";
import "@/components/marvel0/marvel0-styles.css";
import "@/components/marvel0/marvel0-dock.css";
import { useMemo, useState } from "react";
import { Note } from "@/components/pages-ui/feedback";
import { useLocale } from "@/components/levi-locale";
import { ChainStrip } from "@/components/marvel0/chain-strip";
import { chainView } from "@/components/marvel0/chain-logic";
import type { DeployStatus } from "@/components/marvel0/deploy-logic";
import {
  Freshness,
  IdleBannerView,
  Marvel0CardView,
} from "@/components/marvel0/marvel0-card";
import { AutonomyStepLazy } from "@/components/marvel0/autonomy-lazy";
import type { AeriFact } from "@/components/marvel0/autonomy-logic";
import { Marvel0Actions } from "@/components/marvel0/marvel0-actions";
import {
  marvel0View,
  type Marvel0Group,
  type Marvel0GroupId,
} from "@/components/marvel0/marvel0-logic";
import { sayText } from "@/components/marvel0/marvel0-say";
import { RunDock } from "@/components/marvel0/run-dock";
import { runApi } from "@/components/marvel0/run-api";
import { holdEnabled } from "@/components/marvel0/run-logic";
import { StackPanel } from "@/components/marvel0/stack-panel";
import { stackApi } from "@/components/marvel0/stack-api";
import { StudioLink } from "@/components/marvel0/studio-link";
import { useMarvel0Poll } from "@/components/marvel0/use-marvel0-poll";
import { useRunActions } from "@/components/marvel0/use-run-actions";
import { useRunPoll } from "@/components/marvel0/use-run-poll";
import { useStackPoll } from "@/components/marvel0/use-stack-poll";

function Cards({ group }: { group: Marvel0Group | undefined }) {
  return (
    <>
      {(group?.cards ?? []).map((card) => (
        <Marvel0CardView key={card.id} card={card} />
      ))}
    </>
  );
}

export default function Marvel0Page() {
  const { t } = useLocale();
  const poll = useMarvel0Poll();
  const stack = useStackPoll(stackApi);
  const run = useRunPoll(runApi);
  const runActions = useRunActions(runApi, run);
  // The policy server's state is read by its own card (step 2); the chain
  // follows what that card last read.
  const [deploy, setDeploy] = useState<DeployStatus | null>(null);
  // The hosted terminal D (step 5) as the chain needs it: alive or not, and in
  // which state. Step 5 is loaded on demand; until it has been, there is none.
  const [aeri, setAeri] = useState<AeriFact | null>(null);
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
  // The chain shows what was read lately: an answer that is no longer
  // current is left out and its node falls back to the status cards.
  const stackNow = stack.current ? stack.value : null;
  const runNow = run.current ? run.value : null;
  const chain = useMemo(
    () =>
      chainView({
        strip: view.stack,
        stack: stackNow,
        deploy,
        run: runNow,
        aeri,
      }),
    [view.stack, stackNow, deploy, runNow, aeri],
  );
  const group = (id: Marvel0GroupId) =>
    view.groups.find((entry) => entry.id === id);
  const host = group("host");
  // "Robot idle?" guesses whether something uses the arm. While this page's
  // own run is live (the client it hosts may move the arm, or is frozen) the
  // guess only repeats what the Run card says, so it is left out.
  const ownRunLive =
    holdEnabled(run.lastSeen) || run.lastSeen?.state === "held";
  const hostRed = host?.cards.some((card) => card.state === "red") ?? false;
  return (
    <main className="ds-root ds-page pg-workbench rg-page">
      <header className="rg-head">
        <h1>{t("marvel0")}</h1>
        <Freshness
          loaded={poll.loaded}
          lastOk={poll.lastOk}
          delay={poll.delay}
          failures={poll.failures}
          errors={[poll.setupError, poll.marvel0Error].filter(Boolean)}
          onRefresh={poll.refresh}
        />
      </header>
      <Note tone="warning" className="rg-scope-note">
        <strong>{t("marvel0.scope.lead")}</strong>
        <details className="rg-details">
          <summary>{t("marvel0.scope.more")}</summary>
          <ul className="rg-scope">
            <li>{t("marvel0.scope.read_only")}</li>
            <li>{t("marvel0.scope.no_connect")}</li>
            <li>{t("marvel0.scope.actions")}</li>
            <li>{t("marvel0.scope.gates")}</li>
            <li>{t("marvel0.scope.no_estop")}</li>
          </ul>
        </details>
      </Note>
      {/* Offline, no robot connection: there whatever the status polls say. */}
      <StudioLink />
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
          <ChainStrip chain={chain} cause={view.stack.cause} />
          {!ownRunLive && <IdleBannerView banner={view.banner} />}
          <StackPanel
            api={stackApi}
            poll={stack}
            cards={group("arm")?.cards ?? []}
          />
          <Marvel0Actions
            policyStatus={<Cards group={group("policy")} />}
            clientStatus={<Cards group={group("client")} />}
            run={run}
            runActions={runActions}
            onDeploy={setDeploy}
          />
          {/* Step 5: prepare, start and drive the AERI loop (off until the
              control file says so; only one of it and the client at a time). */}
          <AutonomyStepLazy evalLive={ownRunLive} onRun={setAeri} />
          {host && (
            <details
              className="rg-host"
              data-testid="marvel0-host"
              open={hostRed ? true : undefined}
            >
              <summary>
                <h2 id="marvel0-group-host">{t(host.title)}</h2>
              </summary>
              <div className="rg-grid">
                <Cards group={host} />
              </div>
            </details>
          )}
        </>
      )}
      {/* Not behind the status poll: a reload while the client runs must show HOLD at once. */}
      <RunDock poll={run} actions={runActions} />
    </main>
  );
}
