"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import en from "@/i18n/en.json";
import { stackApi, type StackApi } from "@/components/marvel0/stack-api";
import {
  parseStack,
  STACK_FRESH_MS,
} from "@/components/marvel0/use-stack-poll";
import { usePolled } from "@/components/marvel0/use-polled";
import { sayRefusal, STACK_REFUSE } from "@/components/marvel0/ctl-failure";
import type { ApiFailure } from "@/components/marvel0/deploy-api";
import { rec } from "@/components/marvel0/marvel0-json";
import { sayText } from "@/components/marvel0/marvel0-say";

const known = new Set(Object.keys(en));
const ratio = (value: unknown): value is number =>
  typeof value === "number" &&
  Number.isFinite(value) &&
  value >= 0 &&
  value <= 1;
const adjustmentKeys = new Set([
  "ArrowLeft",
  "ArrowRight",
  "ArrowUp",
  "ArrowDown",
  "Home",
  "End",
  "PageUp",
  "PageDown",
]);

/** Reject malformed gates as well as delayed/cached backend samples. */
export function parseGripperStack(data: unknown) {
  const raw = rec(rec(rec(data)?.act)?.gripper_test);
  const parsed = parseStack(data);
  const sampled = parsed?.passive.sampledAt;
  if (
    !raw ||
    typeof raw.can_run !== "boolean" ||
    !Array.isArray(raw.blockers) ||
    raw.blockers.some(
      (item: unknown) => !rec(item) || typeof rec(item)?.code !== "string",
    ) ||
    !["running", "idle"].includes(String(raw.state)) ||
    !parsed?.act ||
    sampled == null ||
    !Number.isFinite(sampled) ||
    sampled < 0 ||
    sampled * 1000 > Date.now() + 1000 ||
    Date.now() - sampled * 1000 > STACK_FRESH_MS
  )
    return null;
  return parsed;
}

export interface GripperControlsProps {
  /** Fresh measured feedback only; the parent must pass null when stale. */
  measuredOpeningRatio: number | null;
  api?: StackApi;
}

export default function GripperControls({
  measuredOpeningRatio,
  api = stackApi,
}: GripperControlsProps) {
  const { t } = useLocale();
  const read = useCallback((signal: AbortSignal) => api.read(signal), [api]);
  const poll = usePolled({
    read,
    parse: parseGripperStack,
    pollMs: 2000,
    freshMs: STACK_FRESH_MS,
  });
  const [target, setTarget] = useState<number | null>(
    ratio(measuredOpeningRatio) ? measuredOpeningRatio : null,
  );
  const targetRef = useRef(target);
  const requestedTarget = useRef(false);
  const editing = useRef(false);
  const pointer = useRef<number | null>(null);
  const key = useRef<string | null>(null);
  const inflight = useRef(false);
  const mounted = useRef(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiFailure | null>(null);
  const [unknown, setUnknown] = useState(false);
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    mounted.current = true;
    const timer = setInterval(() => setNow(Date.now()), 500);
    return () => {
      mounted.current = false;
      clearInterval(timer);
    };
  }, []);
  useEffect(() => {
    if (!editing.current && !requestedTarget.current) {
      const next = ratio(measuredOpeningRatio) ? measuredOpeningRatio : null;
      targetRef.current = next;
      setTarget(next);
    }
  }, [measuredOpeningRatio]);
  const fresh =
    poll.current &&
    poll.lastOk !== null &&
    now - poll.lastOk <= STACK_FRESH_MS &&
    poll.value?.passive.sampledAt != null &&
    now - poll.value.passive.sampledAt * 1000 <= STACK_FRESH_MS;
  const gripper = fresh ? poll.value?.act?.gripper : null;
  const allowed =
    fresh &&
    gripper?.canRun === true &&
    !gripper.running &&
    gripper.blockers.length === 0 &&
    ratio(measuredOpeningRatio) &&
    !busy &&
    !unknown;
  const live = useRef({ allowed, poll });
  live.current = { allowed, poll };
  const cancel = () => {
    if (!editing.current) return;
    requestedTarget.current = false;
    editing.current = false;
    pointer.current = null;
    key.current = null;
    const next = ratio(measuredOpeningRatio) ? measuredOpeningRatio : null;
    targetRef.current = next;
    setTarget(next);
  };
  const cancelRef = useRef(cancel);
  cancelRef.current = cancel;
  useEffect(() => {
    if (!allowed) cancelRef.current();
  }, [allowed]);
  useEffect(() => {
    const cancelGesture = () => cancelRef.current();
    window.addEventListener("blur", cancelGesture);
    document.addEventListener("visibilitychange", cancelGesture);
    return () => {
      window.removeEventListener("blur", cancelGesture);
      document.removeEventListener("visibilitychange", cancelGesture);
    };
  }, []);
  async function send(action: "open" | "close" | "position", opening?: number) {
    const current = live.current;
    if (
      inflight.current ||
      !current.allowed ||
      document.visibilityState === "hidden" ||
      current.poll.lastOk === null ||
      Date.now() - current.poll.lastOk > STACK_FRESH_MS ||
      current.poll.value?.passive.sampledAt == null ||
      Date.now() - current.poll.value.passive.sampledAt * 1000 >
        STACK_FRESH_MS ||
      (action === "position" && !ratio(opening))
    )
      return;
    requestedTarget.current = true;
    const next = action === "open" ? 1 : action === "close" ? 0 : opening!;
    targetRef.current = next;
    setTarget(next);
    inflight.current = true;
    setBusy(true);
    setError(null);
    try {
      const answer = await api.gripper({
        request_id: crypto.randomUUID(),
        action,
        confirm: `gripper ${action}`,
        attest: { hands_clear: true },
        ...(action === "position" ? { opening_ratio: opening } : {}),
      });
      if (!mounted.current) return;
      if (!answer.ok) {
        setError(answer);
        if (answer.code === "network") setUnknown(true);
      }
      await current.poll.refresh();
    } catch {
      if (mounted.current) setUnknown(true);
    } finally {
      inflight.current = false;
      if (mounted.current) setBusy(false);
    }
  }
  const said = error ? sayRefusal(error, STACK_REFUSE, known) : null;
  return (
    <section
      className="rs-panel-section"
      data-testid="studio-gripper-controls"
      aria-busy={busy}
    >
      <h2>{t("robotStudio.gripper.title")}</h2>
      <p className="rs-meta">{t("robotStudio.gripper.singleClick")}</p>
      <p>
        {t("robotStudio.gripper.measured")}{" "}
        <output data-testid="studio-gripper-measured">
          {ratio(measuredOpeningRatio)
            ? `${Math.round(measuredOpeningRatio * 100)}%`
            : "—"}
        </output>
      </p>
      <p className="rs-meta" data-testid="studio-gripper-hands-clear">
        {t("robotStudio.gripper.handsClear")}
      </p>
      <div className="rs-manual-actions">
        <Button
          data-testid="studio-gripper-open"
          size="sm"
          disabled={!allowed}
          onClick={() => void send("open")}
        >
          {t("robotStudio.gripper.open")}
        </Button>
        <Button
          data-testid="studio-gripper-close"
          size="sm"
          disabled={!allowed}
          onClick={() => void send("close")}
        >
          {t("robotStudio.gripper.close")}
        </Button>
      </div>
      <label>
        {t("robotStudio.gripper.target")}
        <input
          data-testid="studio-gripper-target"
          type="range"
          min="0"
          max="1"
          step="0.01"
          value={target ?? 0}
          disabled={!allowed}
          onChange={(e) => {
            if (editing.current) {
              targetRef.current = Number(e.target.value);
              setTarget(targetRef.current);
            }
          }}
          onPointerDown={(e) => {
            if (e.button !== 0 || !allowed) return;
            editing.current = true;
            pointer.current = e.pointerId;
            key.current = null;
            e.currentTarget.setPointerCapture?.(e.pointerId);
          }}
          onPointerUp={(e) => {
            if (pointer.current !== e.pointerId || !editing.current) return;
            const next = targetRef.current;
            editing.current = false;
            pointer.current = null;
            if (ratio(next)) void send("position", next);
          }}
          onPointerCancel={cancel}
          onLostPointerCapture={cancel}
          onBlur={cancel}
          onKeyDown={(e) => {
            if (e.key === "Escape") {
              e.preventDefault();
              cancel();
            } else if (
              adjustmentKeys.has(e.key) &&
              !e.repeat &&
              allowed &&
              pointer.current === null
            ) {
              editing.current = true;
              key.current = e.key;
            }
          }}
          onKeyUp={(e) => {
            if (
              e.repeat ||
              key.current !== e.key ||
              !adjustmentKeys.has(e.key) ||
              !editing.current
            )
              return;
            const next = targetRef.current;
            key.current = null;
            editing.current = false;
            if (ratio(next)) void send("position", next);
          }}
        />
      </label>
      <output data-testid="studio-gripper-target-value">
        {ratio(target) ? `${Math.round(target * 100)}%` : "—"}
      </output>
      {(!fresh || !ratio(measuredOpeningRatio)) && (
        <p role="status">{t("robotStudio.gripper.feedbackRequired")}</p>
      )}
      {gripper?.running || busy ? (
        <p role="status">{t("robotStudio.gripper.busy")}</p>
      ) : null}
      {gripper?.blockers.map((blocker, index) => (
        <p key={index}>
          {blocker.worded.say
            ? sayText(blocker.worded.say, t)
            : blocker.worded.text}
        </p>
      ))}
      {gripper?.last && (
        <p data-testid="studio-gripper-last">
          {t("robotStudio.gripper.last")}{" "}
          <code>{gripper.last.code ?? "—"}</code>
        </p>
      )}
      {said && (
        <p role="alert">{said.say ? sayText(said.say, t) : said.text}</p>
      )}
      {unknown && <p role="alert">{t("robotStudio.gripper.unknown")}</p>}
      {poll.problem && (
        <p role="alert">{t("robotStudio.gripper.readFailed")}</p>
      )}
    </section>
  );
}
