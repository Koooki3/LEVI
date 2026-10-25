"use client";
// What a click on the arm stack's controls does: one write at a time, with
// the request id kept across a retry, "working" from the click until the page
// has read the stack again, and a refusal shown in words from the backend's
// code. The writes are the person's: nothing here starts by itself, and the
// guided sequences are only started by a click on their own buttons. A confirm
// window sends the action's fixed confirmation phrase (the spec's), never
// text a person typed; only the override of a stop of C is typed. Gripper
// open/close clicks acknowledge their inline notice and need no window.
import { useCallback, useRef, useState } from "react";
import { RequestIds } from "@/components/live/live-service-logic";
import en from "@/i18n/en.json";
import { sayRefusal, STACK_REFUSE } from "./ctl-failure";
import type { ApiFailure, ApiResult } from "./deploy-api";
import { newRequestId } from "./deploy-logic";
import type { Worded } from "./marvel0-json";
import type { Say } from "./marvel0-logic";
import {
  gripperBody,
  markerBody,
  preflightBody,
  sequenceBody,
  sequenceStopBody,
  startBody,
  stopBody,
  type MarkerOp,
  type StackApi,
} from "./stack-api";
import {
  attestBody,
  type DialogKind,
  type DialogSpec,
} from "./stack-dialog-logic";
import type { Polled } from "./use-polled";
import type { StackDoc } from "./use-stack-poll";

const KNOWN = new Set(Object.keys(en));

/** The name of one write, for "working" and for where its refusal is shown. */
export const workKey = (
  kind: DialogKind | { kind: "preflight"; comp: "A" | "C" },
): string => {
  switch (kind.kind) {
    case "start":
    case "stop":
      return `${kind.kind}:${kind.comp}`;
    case "marker":
      return `marker:${kind.op}`;
    case "gripper":
      return `gripper:${kind.action}`;
    case "preflight":
      return `preflight:${kind.comp}`;
    case "sequence":
      return "sequence";
    case "sequence_stop":
      return "sequence_stop";
  }
};

/** How a write ended: went through, or the code of its refusal (`network`
 * when no answer came; null when another write was still going on). */
export interface Outcome {
  ok: boolean;
  code: string | null;
}

/** What a window adds to the checks it was ticked with. */
export interface ConfirmInput {
  override: string;
  pause: boolean;
}

export interface Refused {
  key: string;
  failure: ApiFailure;
  worded: Worded<Say>;
}

export interface StackActions {
  /** The write in flight or waiting for the next read, by `workKey`. */
  working: string | null;
  /** The last read of the stack is not current: nothing may be started on
   * what was seen before. */
  locked: boolean;
  /** The last refusal; cleared by the next write. */
  refused: Refused | null;
  /** The last write that went through, by `workKey`. */
  done: string | null;
  /** What the backend said it did for that write (`done` of its answer), when
   * it said: `already_started` and `already_stopped` start or stop nothing. */
  answered: { key: string; done: string } | null;
  /** Sends a confirmed window. `input.override` is the typed override phrase
   * of a stop of C with a client connected, else empty; `input.pause` is the
   * whole stack's start paused before B and before C. */
  confirm: (
    want: DialogKind,
    spec: DialogSpec,
    input: ConfirmInput,
  ) => Promise<Outcome>;
  /** One human open/close click acknowledges only the inline hands-clear notice. */
  gripper: (action: "open" | "close", phrase: string) => Promise<Outcome>;
  /** Runs a check that needs no dialog (A's FCI check, the checks before C). */
  check: (comp: "A" | "C") => Promise<Outcome>;
  /** Ends the guided start or stop; nothing that already runs is stopped. */
  cancelSequence: () => Promise<Outcome>;
  clear: () => void;
}

export function useStackActions(
  api: StackApi,
  poll: Polled<StackDoc>,
): StackActions {
  const ids = useRef(new RequestIds(newRequestId)).current;
  const busy = useRef(false);
  const [working, setWorking] = useState<string | null>(null);
  const [refused, setRefused] = useState<Refused | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const [answered, setAnswered] = useState<{
    key: string;
    done: string;
  } | null>(null);

  const run = useCallback(
    async (key: string, send: (id: string) => Promise<ApiResult>) => {
      if (busy.current) return { ok: false, code: null };
      busy.current = true;
      setWorking(key);
      setRefused(null);
      setDone(null);
      setAnswered(null);
      let answer: ApiResult;
      try {
        answer = await send(ids.next(key));
      } finally {
        busy.current = false;
      }
      if (answer.ok) {
        ids.release();
        setDone(key);
        const said = (answer.data as { done?: unknown } | null)?.done;
        if (typeof said === "string") setAnswered({ key, done: said });
      } else {
        // A refusal is final for its request id. With no answer at all it is
        // not known whether the request arrived: the id is kept, so a press
        // again repeats that request, which the backend never does twice.
        if (answer.status !== 0) ids.release();
        setRefused({
          key,
          failure: answer,
          worded: sayRefusal(answer, STACK_REFUSE, KNOWN),
        });
      }
      // Working lasts until the page has seen the result of the write.
      await poll.refresh();
      setWorking(null);
      return { ok: answer.ok, code: answer.ok ? null : answer.code };
    },
    [ids, poll],
  );

  const confirm = useCallback(
    (want: DialogKind, spec: DialogSpec, { override, pause }: ConfirmInput) => {
      // The confirm click is the confirmation: the page sends the action's
      // fixed phrase itself (the backend still checks it).
      const confirmText = spec.phrase;
      const key = workKey(want);
      switch (want.kind) {
        case "start":
          return run(key, (id) =>
            api.start(want.comp, startBody(id, confirmText, attestBody(spec))),
          );
        case "stop":
          return run(key, (id) =>
            api.stop(want.comp, stopBody(id, confirmText, override.trim())),
          );
        case "marker":
          return run(key, (id) =>
            api.marker(markerBody(id, want.op as MarkerOp, confirmText)),
          );
        case "gripper":
          return run(key, (id) =>
            api.gripper(
              gripperBody(id, want.action, confirmText, attestBody(spec)),
            ),
          );
        case "sequence":
          return run(key, (id) =>
            api.sequence(
              sequenceBody(id, confirmText, attestBody(spec), pause),
            ),
          );
        case "sequence_stop":
          return run(key, (id) =>
            api.sequenceStop(
              sequenceStopBody(id, confirmText, attestBody(spec)),
            ),
          );
      }
    },
    [api, run],
  );

  const gripper = useCallback(
    (action: "open" | "close", phrase: string) =>
      run(workKey({ kind: "gripper", action }), (id) =>
        api.gripper(gripperBody(id, action, phrase, { hands_clear: true })),
      ),
    [api, run],
  );

  const check = useCallback(
    (comp: "A" | "C") =>
      run(workKey({ kind: "preflight", comp }), (id) =>
        api.preflight(comp, preflightBody(id)),
      ),
    [api, run],
  );

  const cancelSequence = useCallback(
    () => run("sequence:cancel", (id) => api.sequenceCancel(preflightBody(id))),
    [api, run],
  );

  const clear = useCallback(() => {
    setRefused(null);
    setDone(null);
    setAnswered(null);
  }, []);

  return {
    working,
    locked: !poll.current,
    refused,
    done,
    answered,
    confirm,
    gripper,
    check,
    cancelSequence,
    clear,
  };
}
