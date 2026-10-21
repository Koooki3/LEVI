"use client";
// The chain at the top of the page: M, Desk, A, B, C, the policy server and the
// evaluation run as connected nodes. A node's colour is its state and the
// state is also written (the colour only repeats it). The first node that is
// not ready is marked and its reason said under the chain; a click on a node
// moves to its card. Nothing here sends a request; what each node is comes
// from chain-logic.ts.
import {
  ArrowRight,
  Check,
  CircleAlert,
  Clock,
  Dot,
  Lock,
  Minus,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useLocale } from "@/components/levi-locale";
import { Badge } from "@/components/ds";
import { WordedText } from "./action-parts";
import {
  NODE_LABEL,
  NODE_TONE,
  nodeAnchor,
  nodeStateKey,
  type Chain,
  type ChainId,
  type FocusKind,
  type NodeState,
} from "./chain-logic";
import type { Say } from "./marvel0-logic";
import { sayText } from "./marvel0-say";

const ICON: Record<NodeState, LucideIcon> = {
  ready: Check,
  working: Clock,
  idle: Minus,
  problem: CircleAlert,
  attention: CircleAlert,
  stale: Clock,
  unknown: Dot,
};

/** The glyph at the head of the line under the chain, by what it says. */
const FOCUS_ICON: Record<FocusKind | "done", LucideIcon> = {
  problem: CircleAlert,
  working: Clock,
  blocked: Lock,
  next: ArrowRight,
  check: CircleAlert,
  done: Check,
};

/** "M · Monitor" is a letter and a name; a node without a letter is only a name. */
function splitLabel(label: string): [string | null, string] {
  const at = label.indexOf(" · ");
  return at < 0 ? [null, label] : [label.slice(0, at), label.slice(at + 3)];
}

const FOCUS_KEY: Record<FocusKind, string> = {
  problem: "marvel0.chain.focus.problem",
  working: "marvel0.chain.focus.working",
  blocked: "marvel0.chain.focus.blocked",
  next: "marvel0.chain.focus.next",
  check: "marvel0.chain.focus.check",
};

/** Moves to an element of the page by its id and puts the keyboard there. */
export function goTo(anchor: string): void {
  const target = document.getElementById(anchor);
  if (!target) return;
  const calm = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  target.scrollIntoView?.({
    block: "start",
    behavior: calm ? "auto" : "smooth",
  });
  target.focus?.({ preventScroll: true });
}

/** Moves to the card of a node. */
export function goToNode(id: ChainId): void {
  goTo(nodeAnchor(id));
}

export function ChainStrip({
  chain,
  cause,
}: {
  chain: Chain;
  /** What several nodes share (the monitor's file stopped updating). */
  cause: Say | null;
}) {
  const { t } = useLocale();
  const focus = chain.focus;
  const ready = chain.nodes.filter((node) => node.state === "ready").length;
  const FocusIcon = FOCUS_ICON[focus?.kind ?? "done"];
  return (
    <section
      className="rg-chain"
      aria-labelledby="marvel0-strip-title"
      data-testid="marvel0-strip"
    >
      <div className="rg-chain__head">
        <h2 id="marvel0-strip-title" className="rg-chain__title">
          {t("marvel0.strip.title")}
        </h2>
        <span className="rg-chain__count" data-testid="marvel0-strip-count">
          {t("marvel0.strip.count")
            .replace("{ready}", String(ready))
            .replace("{total}", String(chain.nodes.length))}
        </span>
      </div>
      {cause && (
        <p className="rg-strip__cause" data-testid="marvel0-strip-cause">
          <Clock aria-hidden="true" size={16} />
          <span>{sayText(cause, t)}</span>
        </p>
      )}
      <ol className="rg-chain__nodes">
        {chain.nodes.map((node, index) => {
          const Icon = ICON[node.state];
          const marked = focus?.id === node.id;
          const [code, name] = splitLabel(t(NODE_LABEL[node.id]));
          return (
            <li
              key={node.id}
              className="rg-chain__item"
              data-testid={`marvel0-strip-${node.id}`}
              data-state={node.state}
              data-detail={node.detail}
              data-focus={marked ? focus!.kind : undefined}
            >
              <button
                type="button"
                className={`rg-node rg-node--${node.state}${marked ? " rg-node--focus" : ""}`}
                aria-current={marked ? "step" : undefined}
                onClick={() => goToNode(node.id)}
              >
                <span className="rg-node__top">
                  <span className="rg-node__glyph" aria-hidden="true">
                    <Icon size={14} />
                  </span>
                  <span className="rg-node__idx" aria-hidden="true">
                    {String(index + 1).padStart(2, "0")}
                  </span>
                </span>
                <span className="rg-node__name">
                  {code && <span className="rg-node__code">{code}</span>}
                  {name}
                </span>
                <Badge tone={NODE_TONE[node.state]} icon={Icon}>
                  {t(
                    node.detail === "test_failed"
                      ? "marvel0.chain.state.ready_test_failed"
                      : nodeStateKey(node.state),
                  )}
                </Badge>
              </button>
              {index < chain.nodes.length - 1 && (
                <span
                  className={`rg-chain__link rg-chain__link--${node.state}`}
                  aria-hidden="true"
                />
              )}
            </li>
          );
        })}
      </ol>
      <p
        className={`rg-chain__focus rg-chain__focus--${focus?.kind ?? "done"}`}
        role="status"
        data-testid="marvel0-chain-focus"
        data-node={focus?.id}
        data-kind={focus?.kind ?? "done"}
      >
        <FocusIcon
          aria-hidden="true"
          size={16}
          className="rg-chain__focus-icon"
        />
        {focus ? (
          <span>
            <strong>
              {sayText(
                {
                  key: FOCUS_KEY[focus.kind],
                  params: { node: { key: NODE_LABEL[focus.id] } },
                },
                t,
              )}
            </strong>
            {focus.reason && (
              <>
                {" "}
                <WordedText worded={focus.reason} />
              </>
            )}
          </span>
        ) : (
          <span>{t("marvel0.chain.focus.done")}</span>
        )}
      </p>
    </section>
  );
}
