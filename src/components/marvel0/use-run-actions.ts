"use client";
// What a click on the hosted client's buttons does. A key is two requests made
// by one click: a one-time challenge for the state the page is looking at,
// then the key with that challenge. The challenge is spent by the key (and
// refused when the run has moved on), so a second click, a stale page or a
// replayed request cannot send the same key twice; the page also keeps its
// buttons off from the click until the next read has come back.
//
// HOLD has no challenge, no phrase and no queue: it is sent at once, whatever
// else is going on, because freezing the client must never wait. There is no
// way to send a raw key from here: only the named actions of run-api.ts.
import { useCallback, useRef, useState } from "react";
import { RequestIds } from "@/components/live/live-service-logic";
import en from "@/i18n/en.json";
import { RUN_REFUSE, sayRefusal } from "./ctl-failure";
import type { ApiResult } from "./deploy-api";
import { newRequestId } from "./deploy-logic";
import { rec, str, type Worded } from "./marvel0-json";
import type { Say } from "./marvel0-logic";
import {
  challengeBody,
  endBody,
  holdBody,
  keyBody,
  startRunBody,
  type KeyAction,
  type RunApi,
} from "./run-api";
import { purposeOf } from "./run-logic";
import type { ApiFailure } from "./deploy-api";
import type { Polled } from "./use-polled";
import type { RunView } from "./run-logic";

const KNOWN = new Set(Object.keys(en));

export interface RunRefused {
  /** What was being done: `start`, an action, `hold` or `end`. */
  key: string;
  failure: ApiFailure;
  worded: Worded<Say>;
}

export interface Ack {
  estop: boolean;
  scene_reset: boolean;
  area_clear: boolean;
}

export interface RunActions {
  /** The key, start or end in flight or waiting for the next read. */
  working: string | null;
  /** A hold was sent and the next read has not come back yet. */
  holding: boolean;
  refused: RunRefused | null;
  press: (action: KeyAction, confirm?: string) => Promise<boolean>;
  hold: () => Promise<boolean>;
  end: (confirm: string) => Promise<boolean>;
  start: (
    form: Record<string, unknown>,
    confirm: string,
    ack: Ack,
  ) => Promise<boolean>;
  clear: () => void;
}

/** The token of a challenge answer, or null when it has none. */
export const challengeOf = (data: unknown): string | null =>
  str(rec(data)?.challenge, 200);

export function useRunActions(api: RunApi, poll: Polled<RunView>): RunActions {
  const writeIds = useRef(new RequestIds(newRequestId)).current;
  const holdIds = useRef(new RequestIds(newRequestId)).current;
  const busy = useRef(false);
  const [working, setWorking] = useState<string | null>(null);
  const [holding, setHolding] = useState(false);
  const [refused, setRefused] = useState<RunRefused | null>(null);

  const fail = useCallback(
    (key: string, answer: ApiFailure) =>
      setRefused({
        key,
        failure: answer,
        worded: sayRefusal(answer, RUN_REFUSE, KNOWN),
      }),
    [],
  );

  /** A write behind a challenge: ask for the challenge, then send. */
  const guarded = useCallback(
    async (
      key: string,
      purpose: NonNullable<ReturnType<typeof purposeOf>>,
      form: Record<string, unknown> | undefined,
      send: (id: string, challenge: string) => Promise<ApiResult>,
    ): Promise<boolean> => {
      if (busy.current) return false;
      busy.current = true;
      setWorking(key);
      setRefused(null);
      let ok = false;
      try {
        const asked = await api.challenge(challengeBody(purpose, form));
        const challenge = asked.ok ? challengeOf(asked.data) : null;
        if (!asked.ok) fail(key, asked);
        else if (!challenge)
          fail(key, {
            ok: false,
            status: asked.status,
            code: "format",
            message: "",
            extra: {},
            field: null,
            fields: [],
          });
        else {
          const answer = await send(writeIds.next(key), challenge);
          if (answer.ok) {
            writeIds.release();
            ok = true;
          } else {
            // No answer at all: the request id stays, so a press again is the
            // same request, which the backend never carries out twice.
            if (answer.status !== 0) writeIds.release();
            fail(key, answer);
          }
        }
      } finally {
        busy.current = false;
      }
      await poll.refresh();
      setWorking(null);
      return ok;
    },
    [api, writeIds, fail, poll],
  );

  const press = useCallback(
    (action: KeyAction, confirm?: string) => {
      const purpose = purposeOf(action)!;
      return guarded(action, purpose, undefined, (id, challenge) =>
        api.key(keyBody(id, action, challenge, confirm)),
      );
    },
    [api, guarded],
  );

  const start = useCallback(
    (form: Record<string, unknown>, confirm: string, ack: Ack) =>
      guarded("start", "start", form, (id, challenge) =>
        api.start(startRunBody(id, form, challenge, confirm, ack)),
      ),
    [api, guarded],
  );

  const end = useCallback(
    async (confirm: string) => {
      if (busy.current) return false;
      busy.current = true;
      setWorking("end");
      setRefused(null);
      let ok = false;
      try {
        const answer = await api.end(endBody(writeIds.next("end"), confirm));
        if (answer.ok) {
          writeIds.release();
          ok = true;
        } else {
          if (answer.status !== 0) writeIds.release();
          fail("end", answer);
        }
      } finally {
        busy.current = false;
      }
      await poll.refresh();
      setWorking(null);
      return ok;
    },
    [api, writeIds, fail, poll],
  );

  /** Freezes the client. Never waits for another write and never refuses to
   * be sent twice: the backend answers a hold of a frozen client as done. */
  const hold = useCallback(async () => {
    setHolding(true);
    let ok = false;
    try {
      const answer = await api.hold(holdBody(holdIds.next("hold")));
      if (answer.ok) {
        holdIds.release();
        ok = true;
      } else {
        if (answer.status !== 0) holdIds.release();
        fail("hold", answer);
      }
    } finally {
      await poll.refresh();
      setHolding(false);
    }
    return ok;
  }, [api, holdIds, fail, poll]);

  const clear = useCallback(() => setRefused(null), []);
  return { working, holding, refused, press, hold, end, start, clear };
}
