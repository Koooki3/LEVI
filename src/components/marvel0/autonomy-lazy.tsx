"use client";
// Step 5 loaded on demand: the page's first paint does not carry the step's
// code (its cards, keys and ~400 sentences), and a step that fails to load, or
// throws while it renders, is replaced by one plain note instead of taking the
// whole page down with it. The page keeps what must always be there without
// Step 5: the chain, the arm stack, the evaluation client and its HOLD.
//
// The note says how to stop a terminal D that is running anyway (the script
// that needs no page, and the physical e-stop): a step that cannot load is
// exactly when its own Stop button is not there.
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

function Failed() {
  const { t } = useLocale();
  return (
    <Note tone="warning" role="alert">
      <span data-testid="marvel0-autonomy-load-failed">
        {t("marvel0.autonomy.load_failed")}
      </span>
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
