"use client";
// The hooks of Step 5: the readiness document (every 3 s), the hosted D's
// document (every second while D lives, every 3 s otherwise; polling stops
// while the tab is hidden: use-polled.ts has the rules), the presence beat of
// a tab that is visible and in focus, the clock a countdown needs, the times
// this page first showed each key, and what a click does.
//
// What a click does, in short. A key is ONE request carrying what the page was
// looking at (`seen`), the key's one-time token and this tab's id; the backend
// compares `seen` with its own state, and sends nothing when they differ. The
// page sends it once: a second click while the first is on its way sends
// nothing (a ref, not state, so two clicks in one frame are still one), a
// request with no answer is never sent again by this page, and no other key is
// offered for the same prompt. There is no optimistic state: what the card
// shows after a click is what the next document says. Stop is the exception
// to the single flight: it is its own path, never waits for a key, and is sent
// once per run (the backend opens one retry when the first got no
// acknowledgement).
import { useCallback, useEffect, useRef, useState } from "react";
import { RequestIds } from "@/components/live/live-service-logic";
import en from "@/i18n/en.json";
import {
  challengeBody,
  confirmBody,
  endBody,
  keyBody,
  presenceBody,
  startBody,
  stopBody,
  type AutonomyApi,
  type StartAttest,
} from "./autonomy-api";
import {
  ERR_PREFIXES,
  readyView,
  runView,
  type KeyView,
  type ReadyView,
  type RunView,
} from "./autonomy-doc";
import {
  LEFTOVERS_PHRASE,
  LIMITS,
  LIVE_STOP_PHRASE,
  READY_FRESH_MS,
  RUN_FRESH_MS,
  armKey,
  historySig,
  prepareBody,
  startPhrase,
  stopSeen,
  type Limits,
  type PrepareForm,
  type SentKey,
  type StopSeen,
  type StopSent,
} from "./autonomy-logic";
import { sayRefusal } from "./ctl-failure";
import type { ApiFailure, ApiResult } from "./deploy-api";
import { newRequestId } from "./deploy-logic";
import { rec, str, type Worded } from "./marvel0-json";
import type { Say } from "./marvel0-logic";
import { usePolled, type Polled } from "./use-polled";

const KNOWN = new Set(Object.keys(en));

// -- polls

export function useAutonomyReady(
  api: AutonomyApi,
  { pollMs = LIMITS.readyPollMs }: { pollMs?: number } = {},
): Polled<ReadyView> {
  const read = useCallback((signal: AbortSignal) => api.ready(signal), [api]);
  const parse = useCallback((data: unknown) => readyView(data, KNOWN), []);
  return usePolled<ReadyView>({
    read,
    parse,
    pollMs,
    freshMs: READY_FRESH_MS,
  });
}

export interface AutonomyRunPoll extends Polled<RunView> {
  /** What the last good read said about Stop, kept however old it is; null
   * until one was read. */
  lastSeen: StopSeen | null;
}

export function useAutonomyRun(
  api: AutonomyApi,
  limits: Limits = LIMITS,
): AutonomyRunPoll {
  const seen = useRef<StopSeen | null>(null);
  const [pace, setPace] = useState(limits.idlePollMs);
  const read = useCallback((signal: AbortSignal) => api.run(signal), [api]);
  const parse = useCallback((data: unknown) => {
    const view = runView(data, KNOWN);
    if (view) seen.current = stopSeen(view);
    return view;
  }, []);
  const polled = usePolled<RunView>({
    read,
    parse,
    pollMs: pace,
    freshMs: RUN_FRESH_MS,
  });
  // One read a second while D lives (its prompts move), every 3 s otherwise.
  const alive = polled.value?.d.alive === true;
  useEffect(() => {
    setPace(alive ? limits.runPollMs : limits.idlePollMs);
  }, [alive, limits.runPollMs, limits.idlePollMs]);
  return { ...polled, lastSeen: seen.current };
}

// -- presence

/** An id for this browser tab, new on every load. */
export function newTabId(): string {
  try {
    return crypto.randomUUID();
  } catch {
    return `tab-${Math.random().toString(36).slice(2)}${Date.now().toString(36)}`;
  }
}

/** Tells the backend this tab is looking at the page: a beat at once and then
 * every `presenceMs`, only while the tab is visible and in focus (a hidden or
 * unfocused tab sends nothing; a hide sends one `visible: false`). The key
 * routes refuse a tab whose last beat is older than 15 s, so a tab left open
 * in a background window cannot click. Failures are not shown: the document's
 * own `presence` says whether the backend hears this tab. */
export function usePresence(
  api: AutonomyApi,
  active: boolean,
  tabId: string,
  limits: Limits = LIMITS,
): void {
  useEffect(() => {
    if (!active) return;
    let timer: ReturnType<typeof setInterval> | undefined;
    const beat = (visible: boolean) => {
      void api.presence(presenceBody(tabId, visible)).catch(() => {});
    };
    const looking = () =>
      document.visibilityState !== "hidden" &&
      (typeof document.hasFocus !== "function" || document.hasFocus());
    const start = () => {
      stop();
      if (!looking()) return;
      beat(true);
      timer = setInterval(() => {
        if (looking()) beat(true);
      }, limits.presenceMs);
    };
    const stop = () => {
      if (timer !== undefined) clearInterval(timer);
      timer = undefined;
    };
    const onHide = () => {
      if (timer !== undefined) beat(false);
      stop();
    };
    const onVisibility = () =>
      document.visibilityState === "hidden" ? onHide() : start();
    start();
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("focus", start);
    window.addEventListener("blur", onHide);
    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("focus", start);
      window.removeEventListener("blur", onHide);
    };
  }, [api, active, tabId, limits.presenceMs]);
}

// -- the clock and the first times

/** The time, re-read every `ms` while `active` (a countdown or a sent key
 * waiting to be seen taken); otherwise the time of the render. */
export function useClock(active: boolean, ms = 100): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(timer);
  }, [active, ms]);
  return active ? now : Date.now();
}

/** When this page first showed each allowed key (by `armKey`): a key is new
 * whenever the run, the state counter or the key is, and one that stops being
 * allowed is forgotten, so it starts its delay again when it comes back. */
export function useArming(run: RunView | null): ReadonlyMap<string, number> {
  const map = useRef(new Map<string, number>()).current;
  useEffect(() => {
    const live = new Set<string>();
    if (run)
      for (const key of run.keys) {
        if (!key.allowed) continue;
        const id = armKey(run, key);
        live.add(id);
        if (!map.has(id)) map.set(id, Date.now());
      }
    for (const id of [...map.keys()]) if (!live.has(id)) map.delete(id);
  }, [run, map]);
  return map;
}

// -- what a click does

export interface AutonomyRefused {
  /** What was being done: a key's action, `stop`, `end`, `start`, `prepare`,
   * `live_stop` or `leftovers_stop`. */
  key: string;
  failure: ApiFailure;
  worded: Worded<Say>;
}

export interface AutonomyActions {
  /** The write in flight (a key's action, `start`, `prepare`, ...). */
  working: string | null;
  /** The key this page sent for the prompt it is looking at. */
  sent: SentKey | null;
  /** When this page last sent a key (epoch ms): the 3 s gap counts from it. */
  lastKeyAt: number | null;
  /** A Stop is on its way. */
  stopping: boolean;
  /** The Stop this page sent, until the stop phase moves on. */
  stopSent: StopSent | null;
  refused: AutonomyRefused | null;
  tabId: string;
  pressKey: (key: KeyView, run: RunView, phrase?: string) => Promise<boolean>;
  stop: () => Promise<boolean>;
  end: (phrase: string) => Promise<boolean>;
  start: (name: string, attest: StartAttest) => Promise<boolean>;
  prepare: (form: PrepareForm) => Promise<boolean>;
  stopLive: () => Promise<boolean>;
  stopLeftovers: () => Promise<boolean>;
  clear: () => void;
}

/** Refusals after which the backend holds a lock for the prompt: the page
 * shows the key as sent at once instead of waiting for the next read. */
const LOCKING = new Set(["key_sent", "key_unconfirmed", "verdict_once"]);

export function useAutonomyActions(
  api: AutonomyApi,
  run: AutonomyRunPoll,
  ready: Polled<ReadyView>,
  { tabId }: { tabId: string },
): AutonomyActions {
  const ids = useRef(new RequestIds(newRequestId)).current;
  const stopIds = useRef(new RequestIds(newRequestId)).current;
  const busy = useRef(false);
  const stopBusy = useRef(false);
  const runRef = useRef(run);
  runRef.current = run;
  const [working, setWorking] = useState<string | null>(null);
  const [sent, setSent] = useState<SentKey | null>(null);
  const [lastKeyAt, setLastKeyAt] = useState<number | null>(null);
  const [stopping, setStopping] = useState(false);
  const [stopSent, setStopSent] = useState<StopSent | null>(null);
  const [refused, setRefused] = useState<AutonomyRefused | null>(null);

  const fail = useCallback((key: string, answer: ApiFailure) => {
    setRefused({
      key,
      failure: answer,
      worded: sayRefusal(answer, ERR_PREFIXES, KNOWN),
    });
  }, []);

  /** One single-flight write: nothing else of the card is sent meanwhile. */
  const guarded = useCallback(
    async (
      key: string,
      send: () => Promise<boolean>,
      after: () => Promise<unknown>,
    ): Promise<boolean> => {
      if (busy.current) return false;
      busy.current = true;
      setWorking(key);
      setRefused(null);
      let ok = false;
      try {
        ok = await send();
      } finally {
        busy.current = false;
      }
      await after();
      setWorking(null);
      return ok;
    },
    [],
  );

  const pressKey = useCallback(
    (key: KeyView, view: RunView, phrase?: string) => {
      const runId = view.runId;
      const promptId = view.state.prompt?.id ?? null;
      // A key answers a prompt: without the prompt the page was looking at,
      // the backend has nothing to compare with and the page sends nothing.
      if (!runId || !key.token || promptId === null) {
        fail(key.id, {
          ok: false,
          status: 0,
          code: "stale_page",
          message: "",
          extra: {},
          field: null,
          fields: [],
        });
        return Promise.resolve(false);
      }
      const record: SentKey = {
        runId,
        seq: view.state.seq,
        action: key.id,
        at: Date.now(),
        histSig: historySig(view),
        lost: false,
      };
      return guarded(
        key.id,
        async () => {
          const answer: ApiResult = await api.key(
            keyBody(
              ids.next(key.id),
              key.id,
              {
                run_id: runId,
                state_seq: view.state.seq,
                prompt_id: promptId,
              },
              key.token as string,
              tabId,
              key.level === "C" ? phrase : undefined,
            ),
          );
          if (answer.ok) {
            ids.release();
            setSent(record);
            setLastKeyAt(Date.now());
            return true;
          }
          if (answer.status === 0) {
            // No answer: the key may have arrived. This page never sends it
            // again (the backend would answer a repeat of the same request
            // id, never a second byte); it says so and waits for the log.
            setSent({ ...record, lost: true });
            setLastKeyAt(Date.now());
          } else {
            ids.release();
            if (LOCKING.has(answer.code)) setSent(record);
          }
          fail(key.id, answer);
          return false;
        },
        () => run.refresh(),
      );
    },
    [api, ids, tabId, guarded, fail, run],
  );

  /** Stop is not behind the single flight: it waits for no key, no start and
   * no read, and a double click is one request (the ref, then `stopSent`). */
  const stop = useCallback(async () => {
    if (stopBusy.current) return false;
    const seen = runRef.current.lastSeen;
    stopBusy.current = true;
    setStopping(true);
    let ok = false;
    try {
      const answer = await api.stop(stopBody(stopIds.next("stop")));
      if (answer.ok) {
        stopIds.release();
        setStopSent({
          runId: seen?.runId ?? null,
          phase: seen?.phase ?? "idle",
        });
        ok = true;
      } else {
        // No answer: the same request id goes out again on the next press,
        // which the backend never carries out twice.
        if (answer.status !== 0) stopIds.release();
        if (answer.code === "stop_spent")
          setStopSent({
            runId: seen?.runId ?? null,
            phase: seen?.phase ?? "idle",
          });
        fail("stop", answer);
      }
    } finally {
      stopBusy.current = false;
      await runRef.current.refresh();
      setStopping(false);
    }
    return ok;
  }, [api, stopIds, fail]);

  const write = useCallback(
    (
      key: string,
      call: (id: string) => Promise<ApiResult>,
      refresh: Array<() => Promise<unknown>>,
    ) =>
      guarded(
        key,
        async () => {
          const answer = await call(ids.next(key));
          if (answer.ok) {
            ids.release();
            return true;
          }
          if (answer.status !== 0) ids.release();
          fail(key, answer);
          return false;
        },
        () => Promise.all(refresh.map((refreshOne) => refreshOne())),
      ),
    [guarded, ids, fail],
  );

  const end = useCallback(
    (phrase: string) =>
      write("end", (id) => api.end(endBody(id, phrase)), [() => run.refresh()]),
    [api, write, run],
  );

  const start = useCallback(
    (name: string, attest: StartAttest) =>
      guarded(
        "start",
        async () => {
          const asked = await api.challenge(challengeBody(name));
          const challenge = asked.ok
            ? str(rec(asked.data)?.challenge, 200)
            : null;
          if (!asked.ok) {
            fail("start", asked);
            return false;
          }
          if (!challenge) {
            fail("start", {
              ok: false,
              status: asked.status,
              code: "format",
              message: "",
              extra: {},
              field: null,
              fields: [],
            });
            return false;
          }
          const answer = await api.start(
            startBody(
              ids.next("start"),
              name,
              challenge,
              startPhrase(name, runRef.current.value?.start.phrase),
              attest,
            ),
          );
          if (answer.ok) {
            ids.release();
            return true;
          }
          if (answer.status !== 0) ids.release();
          fail("start", answer);
          return false;
        },
        () => Promise.all([run.refresh(), ready.refresh()]),
      ),
    [api, guarded, ids, fail, run, ready],
  );

  const prepare = useCallback(
    (form: PrepareForm) =>
      write(
        "prepare",
        (id) =>
          api.prepare(prepareBody(id, form, ready.value?.confirm.prepare)),
        [() => ready.refresh()],
      ),
    [api, write, ready],
  );

  const stopLive = useCallback(
    () =>
      write(
        "live_stop",
        (id) =>
          api.liveStop(
            confirmBody(id, ready.value?.confirm.liveStop ?? LIVE_STOP_PHRASE),
          ),
        [() => ready.refresh()],
      ),
    [api, write, ready],
  );

  const stopLeftovers = useCallback(
    () =>
      write(
        "leftovers_stop",
        (id) =>
          api.leftoversStop(
            confirmBody(id, ready.value?.confirm.leftovers ?? LEFTOVERS_PHRASE),
          ),
        [() => ready.refresh()],
      ),
    [api, write, ready],
  );

  const clear = useCallback(() => setRefused(null), []);
  return {
    working,
    sent,
    lastKeyAt,
    stopping,
    stopSent,
    refused,
    tabId,
    pressKey,
    stop,
    end,
    start,
    prepare,
    stopLive,
    stopLeftovers,
    clear,
  };
}
