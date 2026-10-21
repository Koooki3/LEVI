"use client";
// Step 5 loaded on demand: the page's first paint does not carry the step's
// code (its cards, keys and ~400 sentences), and a step that fails to load, or
// throws while it renders, is replaced by one plain note instead of taking the
// whole page down with it. The page keeps what must always be there without
// Step 5: the chain, the arm stack, the evaluation client and its HOLD.
//
// The note says how to stop a terminal D that is running anyway (the script
// that needs no page, with its exact command and what it needs, and the
// physical e-stop): a step that cannot load is exactly when its own Stop button
// is not there. The note is drawn here, in the part of the page that is always
// loaded, from catalogue sentences; it needs nothing from the step's chunk.
import {
  Component,
  Suspense,
  lazy,
  type ComponentProps,
  type ReactNode,
} from "react";
import { useLocale } from "@/components/levi-locale";
import { Note } from "@/components/pages-ui/feedback";
import type { AutonomyStep } from "./autonomy-step";

const Step = lazy(() =>
  import("./autonomy-step").then((module) => ({
    default: module.AutonomyStep,
  })),
);

/** The command that works without the page: one SIGINT to the hosted D, read
 * from the page's own run record (`scripts/aeri_page_stop.py`, run in the LEVI
 * checkout the page is served from). */
export const STOP_SCRIPT_COMMAND = "python3 -I scripts/aeri_page_stop.py";

function Failed() {
  const { t } = useLocale();
  return (
    <Note tone="warning" role="alert">
      <div data-testid="marvel0-autonomy-load-failed" className="rg-aloadfail">
        <p>{t("marvel0.autonomy.load_failed")}</p>
        <p>{t("marvel0.autonomy.load_failed_stop")}</p>
        <pre
          className="rg-command"
          lang="en"
          data-testid="marvel0-autonomy-load-failed-cmd"
        >
          {STOP_SCRIPT_COMMAND}
        </pre>
        <p>{t("marvel0.autonomy.load_failed_needs")}</p>
        <p>
          <strong>{t("marvel0.autonomy.load_failed_estop")}</strong>
        </p>
      </div>
    </Note>
  );
}

function Loading() {
  const { t } = useLocale();
  return (
    <p
      role="status"
      aria-busy="true"
      className="rg-muted"
      data-testid="marvel0-autonomy-loading"
    >
      {t("marvel0.autonomy.loading")}
    </p>
  );
}

export class AutonomyBoundary extends Component<
  { children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };
  static getDerivedStateFromError(): { failed: boolean } {
    return { failed: true };
  }
  render(): ReactNode {
    return this.state.failed ? <Failed /> : this.props.children;
  }
}

export function AutonomyStepLazy(props: ComponentProps<typeof AutonomyStep>) {
  return (
    <AutonomyBoundary>
      <Suspense fallback={<Loading />}>
        <Step {...props} />
      </Suspense>
    </AutonomyBoundary>
  );
}
