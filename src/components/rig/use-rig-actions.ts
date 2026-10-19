"use client";
// Reading for the Rig console's action cards.
//
//   * `useRigCatalog`: the model list and the site view, once when the cards
//     open, again on a press of Reload and when the tab returns after an
//     error or a long absence (the site file may have been installed meanwhile);
//   * `useDeployPoll`: the policy server's state, like `useRigPoll` does for
//     the status cards: every 3 s while the tab is visible, nothing while it
//     is hidden and an immediate read when it returns, a wait that doubles up
//     to 30 s after a failure, a request given up at its deadline, and an
//     answer older than RIG_FRESH_MS dropped when its successor fails, so the
//     card never shows an old state as current. The unit's log is read in the
//     same round while it matters (`wantLog`).
//
// Only LEVI's own routes are asked, through the same-origin proxy that adds
// the person's token; nothing here reads one and nothing connects to a robot
// side port.
import { useCallback, useEffect, useRef, useState } from "react";
import type { ApiFailure, RigApi } from "./deploy-api";
import {
  readModels,
  readSite,
  type ModelsView,
  type SiteView,
} from "./catalog-logic";
import {
  LOG_LINES,
  deployDelay,
  readDeploy,
  readLog,
  wantLog,
  type DeployStatus,
  type LogView,
} from "./deploy-logic";
import { RIG_FRESH_MS, RIG_POLL_MS } from "./rig-logic";

/** A read that did not give a usable answer: the server refused or did not
 * answer, or answered in a format this page does not know. */
export type ReadProblem = ApiFailure | { format: true };

export const isFormat = (problem: ReadProblem): problem is { format: true } =>
  "format" in problem;

/** A parser that fails on an answer it did not expect gives no answer, so the
 * reader says "format" and the polling goes on. */
function parsed<T>(read: () => T | null): T | null {
  try {
    return read();
  } catch {
    return null;
  }
}

/** After this long away, the tab re-reads the catalog when it returns. */
const CATALOG_STALE_MS = 30_000;

export interface RigCatalog {
  loaded: boolean;
  models: ModelsView | null;
  site: SiteView | null;
  modelsProblem: ReadProblem | null;
  siteProblem: ReadProblem | null;
  reload: () => void;
}

export function useRigCatalog(api: RigApi): RigCatalog {
  const again = useRef<() => void>(() => {});
  const reload = useCallback(() => again.current(), []);
  const [state, setState] = useState<Omit<RigCatalog, "reload">>({
    loaded: false,
    models: null,
    site: null,
    modelsProblem: null,
    siteProblem: null,
  });
  useEffect(() => {
    let stopped = false;
    let controller: AbortController | undefined;
    let at = 0;
    let broken = false;
    async function load() {
      controller?.abort();
      controller = new AbortController();
      const signal = controller.signal;
      const both = await Promise.all([
        api.models(signal),
        api.site(signal),
      ]).catch(
        () => null, // aborted: a newer read, or the page left
      );
      if (!both || stopped || signal.aborted) return;
      const [m, s] = both;
      const models = m.ok ? parsed(() => readModels(m.data)) : null;
      const site = s.ok ? parsed(() => readSite(s.data)) : null;
      const modelsProblem: ReadProblem | null = m.ok
        ? models
          ? null
          : { format: true }
        : m;
      const siteProblem: ReadProblem | null = s.ok
        ? site
          ? null
          : { format: true }
        : s;
      at = Date.now();
      broken = modelsProblem !== null || siteProblem !== null;
      setState({ loaded: true, models, site, modelsProblem, siteProblem });
    }
    again.current = () => void load();
    const onVisibility = () => {
      if (
        document.visibilityState !== "hidden" &&
        (broken || Date.now() - at > CATALOG_STALE_MS)
      )
        void load();
    };
    document.addEventListener("visibilitychange", onVisibility);
    void load();
    return () => {
      stopped = true;
      controller?.abort();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [api]);
  return { ...state, reload };
}

export interface DeployPoll {
  /** The first round of reads has finished (either way). */
  loaded: boolean;
  /** The last state read, dropped when it is too old to be current. */
  status: DeployStatus | null;
  /** The last read worked: the state is current, so the card may act on it. */
  current: boolean;
  problem: ReadProblem | null;
  log: LogView | null;
  logProblem: ReadProblem | null;
  /** Epoch milliseconds of the last state read. */
  lastOk: number | null;
  /** Reads again at once (after a write). */
  refresh: () => void;
}

export function useDeployPoll(
  api: RigApi,
  { pollMs = RIG_POLL_MS, logOpen = false } = {},
): DeployPoll {
  const again = useRef<() => void>(() => {});
  const refresh = useCallback(() => again.current(), []);
  const logWanted = useRef(logOpen);
  const [state, setState] = useState<Omit<DeployPoll, "refresh">>({
    loaded: false,
    status: null,
    current: false,
    problem: null,
    log: null,
    logProblem: null,
    lastOk: null,
  });
  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController | undefined;
    let failures = 0;
    let lastOk: number | null = null;
    let status: DeployStatus | null = null;
    let log: LogView | null = null;
    const visible = () => document.visibilityState !== "hidden";
    const schedule = () => {
      if (stopped || !visible()) return;
      timer = setTimeout(() => void tick(), deployDelay(failures, pollMs));
    };
    // An answer too old to be shown as current is not kept.
    const expire = (now: number) => {
      if (lastOk === null || now - lastOk > RIG_FRESH_MS) status = null;
    };
    async function tick() {
      if (stopped || !visible()) return;
      controller = new AbortController();
      const signal = controller.signal;
      let problem: ReadProblem | null = null;
      let logProblem: ReadProblem | null = null;
      const result = await api.deploy(signal).catch(() => null);
      if (!result || stopped || signal.aborted) return; // aborted: see above
      const now = Date.now();
      const read = result.ok ? parsed(() => readDeploy(result.data)) : null;
      if (read) {
        status = read;
        lastOk = now;
        failures = 0;
      } else {
        problem = result.ok ? { format: true } : result;
        failures += 1;
        expire(now);
      }
      if (status && wantLog(status, logWanted.current)) {
        const answer = await api.log(LOG_LINES, signal).catch(() => null);
        if (!answer || stopped || signal.aborted) return;
        const view = answer.ok ? parsed(() => readLog(answer.data)) : null;
        if (view) log = view;
        else logProblem = answer.ok ? { format: true } : answer;
      } else if (!status?.deploy) log = null;
      setState({
        loaded: true,
        status,
        current: problem === null,
        problem,
        log,
        logProblem,
        lastOk,
      });
      schedule();
    }
    const onVisibility = () => {
      clearTimeout(timer);
      if (visible()) {
        // An answer from before the tab was hidden is not current.
        expire(Date.now());
        setState((s) => ({ ...s, status, current: false }));
        void tick();
      } else controller?.abort();
    };
    again.current = () => {
      clearTimeout(timer);
      controller?.abort();
      void tick();
    };
    document.addEventListener("visibilitychange", onVisibility);
    void tick();
    return () => {
      stopped = true;
      clearTimeout(timer);
      controller?.abort();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [api, pollMs]);
  // Opening the log asks for it at once.
  useEffect(() => {
    if (logWanted.current !== logOpen) {
      logWanted.current = logOpen;
      if (logOpen) again.current();
    }
  }, [logOpen]);
  return { ...state, refresh };
}
