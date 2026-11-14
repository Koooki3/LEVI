"use client";
// Polling for the live page. Read-only GETs, no websocket.
//   * 2 s while an evaluation runs, 10 s otherwise;
//   * nothing while the tab is hidden, an immediate refresh when it returns;
//   * after a failure the wait doubles (up to 30 s);
//   * with no live service to show, once a minute;
//   * a dataset's detail is fetched only for expanded rows, and again only
//     when its row in the status changed.
import { useCallback, useEffect, useRef, useState } from "react";
import {
  DETAIL_REFRESH_MS,
  isEvaluating,
  nextDelay,
  rowSignature,
} from "./live-logic";
import { PULSE_OFF_MS } from "./live-pulse-store";
import type {
  DatasetDetail,
  DatasetRow,
  LiveSessionsResponse,
  LiveStatusResponse,
} from "./types";

async function get<T>(path: string, signal: AbortSignal): Promise<T> {
  const response = await fetch(`/api/levi/live/${path}`, {
    cache: "no-store",
    signal,
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

export interface LivePoll {
  status: LiveStatusResponse | null;
  sessions: LiveSessionsResponse | null;
  /** The last request failed: both endpoints, or the one named in `partial`. */
  error: string;
  /** Consecutive polls in which both requests failed (the LEVI core is not
   * answering). */
  failures: number;
  /** Epoch milliseconds of the last poll in which either request succeeded. */
  lastOk: number | null;
  /** Epoch milliseconds of the last good answer of each endpoint, so the page
   * can take the FR3 state and the sessions from the fresher one. */
  statusOkAt: number | null;
  sessionsOkAt: number | null;
  /** The endpoint that failed in the last poll while the other answered
   * ("" when both answered or both failed). Its data is the last good one. */
  partial: "" | "status" | "sessions";
  /** Milliseconds to the next request, for display: the wait actually
   * scheduled (once a minute while no live service is set up). */
  delay: number;
  /** Refresh summary lists immediately after a management action. */
  refresh: () => void;
}

type PollState = Omit<LivePoll, "refresh">;

/** The wait before the next request. With no live service to show (the
 * explanation page) the page looks again once a minute, so one started later
 * appears without a reload. */
export function pollDelay(
  status: LiveStatusResponse | null,
  evaluating: boolean,
  failures: number,
): number {
  return status?.enabled === false && failures === 0
    ? PULSE_OFF_MS
    : nextDelay(evaluating, failures);
}

/** Fast polling only while something is happening: a live session, or a
 * background service that is alive and working (a dead service's last status
 * file still says `annotating`). */
export function pollEvaluating(
  status: LiveStatusResponse | null,
  sessions: LiveSessionsResponse | null,
): boolean {
  return isEvaluating(
    sessions?.sessions ?? status?.service?.sessions ?? [],
    status?.alive ? status.service : null,
  );
}

/** One poll's results folded into the state. Pure, so the partial-failure
 * rules are tested without timers. */
export function foldPoll(
  previous: PollState,
  status: PromiseSettledResult<LiveStatusResponse>,
  sessions: PromiseSettledResult<LiveSessionsResponse>,
  now: number,
): PollState {
  const message = (r: PromiseSettledResult<unknown>) =>
    r.status === "rejected"
      ? (r.reason instanceof Error ? r.reason.message : "") || "Request failed"
      : "";
  if (status.status === "rejected" && sessions.status === "rejected") {
    const failures = previous.failures + 1;
    return {
      ...previous,
      error: message(status),
      failures,
      partial: "",
      delay: pollDelay(
        previous.status,
        pollEvaluating(previous.status, previous.sessions),
        failures,
      ),
    };
  }
  const next: PollState = {
    ...previous,
    error: message(status) || message(sessions),
    failures: 0,
    lastOk: now,
    partial:
      status.status === "rejected"
        ? "status"
        : sessions.status === "rejected"
          ? "sessions"
          : "",
  };
  if (status.status === "fulfilled") {
    next.status = status.value;
    next.statusOkAt = now;
  }
  if (sessions.status === "fulfilled") {
    next.sessions = sessions.value;
    next.sessionsOkAt = now;
  }
  next.delay = pollDelay(
    next.status,
    pollEvaluating(next.status, next.sessions),
    0,
  );
  return next;
}

const INITIAL_POLL: PollState = {
  status: null,
  sessions: null,
  error: "",
  failures: 0,
  lastOk: null,
  statusOkAt: null,
  sessionsOkAt: null,
  partial: "",
  delay: nextDelay(false, 0),
};

export function useLivePoll(): LivePoll {
  const [revision, setRevision] = useState(0);
  const refresh = useCallback(() => setRevision((value) => value + 1), []);
  const [state, setState] = useState<PollState>(INITIAL_POLL);
  // The newest state outside React's batching: a refresh restarts the loop
  // from it, so a failed first request after a deletion keeps the last good
  // snapshot instead of wiping the page.
  const latest = useRef<PollState>(INITIAL_POLL);
  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController | undefined;
    const visible = () => document.visibilityState !== "hidden";
    const schedule = () => {
      if (stopped || !visible()) return;
      timer = setTimeout(() => void tick(), latest.current.delay);
    };
    async function tick() {
      if (stopped || !visible()) return;
      controller = new AbortController();
      const signal = controller.signal;
      const [a, b] = await Promise.allSettled([
        get<LiveStatusResponse>("status", signal),
        get<LiveSessionsResponse>("sessions", signal),
      ]);
      if (stopped || signal.aborted) return;
      latest.current = foldPoll(latest.current, a, b, Date.now());
      setState(latest.current);
      schedule();
    }
    const onVisibility = () => {
      clearTimeout(timer);
      if (visible()) void tick();
      else controller?.abort();
    };
    document.addEventListener("visibilitychange", onVisibility);
    void tick();
    return () => {
      stopped = true;
      clearTimeout(timer);
      controller?.abort();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [revision]);
  return { ...state, refresh };
}

export interface DetailEntry {
  data: DatasetDetail | null;
  error: string;
  /** Epoch milliseconds of the request. */
  at: number;
  signature: string;
}

/** Details of the datasets in `wanted` (card order), refreshed when their
 * row changes and, for an open card that is being annotated, every 10 s. */
export function useDatasetDetails(
  wanted: string[],
  rows: Record<string, DatasetRow>,
  open: Set<string>,
  enabled: boolean,
): {
  details: Record<string, DetailEntry>;
  /** Fetch one dataset's detail again now (after a removal or a restore). */
  refresh: (name: string) => void;
} {
  const [details, setDetails] = useState<Record<string, DetailEntry>>({});
  const inflight = useRef(new Set<string>());
  const known = useRef<Record<string, DetailEntry>>({});
  const [beat, setBeat] = useState(0);

  const fetchOne = useCallback(async (name: string, signature: string) => {
    inflight.current.add(name);
    const controller = new AbortController();
    try {
      const data = await get<DatasetDetail>(
        `datasets/${encodeURIComponent(name)}`,
        controller.signal,
      );
      known.current[name] = { data, error: "", at: Date.now(), signature };
    } catch (e) {
      known.current[name] = {
        data: known.current[name]?.data ?? null,
        error: e instanceof Error ? e.message : String(e),
        at: Date.now(),
        signature,
      };
    } finally {
      inflight.current.delete(name);
    }
    setDetails({ ...known.current });
    // A completion frees one of the three request slots. Re-check wanted
    // rows so a batch of simultaneously expanded rows cannot be stranded.
    setBeat((value) => value + 1);
  }, []);

  const key = wanted
    .map((n) => `${n}:${rowSignature(rows[n])}:${open.has(n) ? 1 : 0}`)
    .join(";");
  useEffect(() => {
    if (!enabled) return;
    for (const name of wanted) {
      if (inflight.current.size >= 3) break;
      if (!open.has(name) || inflight.current.has(name) || !rows[name])
        continue;
      const signature = rowSignature(rows[name]);
      const entry = known.current[name];
      const stale =
        !entry ||
        entry.signature !== signature ||
        (open.has(name) &&
          rows[name].state === "annotating" &&
          Date.now() - entry.at > DETAIL_REFRESH_MS);
      if (stale) {
        void fetchOne(name, signature);
      }
    }
    // `key` stands for wanted, rows and open.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, enabled, beat, fetchOne]);

  // Slow beat so an open, annotating card keeps moving; nothing while hidden.
  const anyBusy = wanted.some(
    (n) => open.has(n) && rows[n]?.state === "annotating",
  );
  useEffect(() => {
    if (!enabled || !anyBusy) return;
    const timer = setInterval(() => {
      if (document.visibilityState !== "hidden") setBeat((b) => b + 1);
    }, DETAIL_REFRESH_MS);
    return () => clearInterval(timer);
  }, [enabled, anyBusy]);
  const refresh = useCallback(
    (name: string) => {
      if (!inflight.current.has(name))
        void fetchOne(name, known.current[name]?.signature ?? "");
    },
    [fetchOne],
  );
  return { details, refresh };
}
