"use client";
// The voice card's behaviour: it joins the microphone (voice-capture.ts), the
// voice routes (speech-api.ts) and the pure rules (speech-logic.ts).
//
//   * Opening the page only reads the voice state (one GET). The microphone is
//     opened by a press on the card's button and by nothing else; a button that
//     the state does not allow does not even start a capture.
//   * Every request (a recording, an edited text, a model switch) gets the next
//     sequence number and aborts the one before it; an answer that comes back
//     for another number is dropped, so a slow answer cannot replace a newer
//     one.
//   * A proposal is armed for Use after 800 ms and expires after 60 s.
//   * Leaving the page aborts what is in flight and releases the microphone.
//
// Nothing here writes a field, starts the client, or touches the clipboard:
// the one thing it hands out is `onUse(sentence)`, and only for a sentence of
// the allowed list that the person pressed Use for.
import {
  useCallback,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
} from "react";
import { newRequestId } from "./deploy-logic";
import type { ApiFailure } from "./deploy-api";
import type { SpeechApi } from "./speech-api";
import {
  PROPOSAL_TTL_MS,
  USE_DELAY_MS,
  availability,
  canUse,
  initialSpeech,
  readHeard,
  readMatched,
  readSpeechStatus,
  speechReducer,
  type Availability,
  type SpeechAction,
  type SpeechState,
  type StatusRead,
} from "./speech-logic";
import {
  VoiceCapture,
  attachPushToTalk,
  dbToFraction,
  micSupport,
  type CaptureEvents,
  type CaptureLike,
  type Clip,
} from "./voice-capture";

/** After this long away, the tab reads the voice state again when it returns. */
const STATUS_STALE_MS = 30_000;

export type MicSupport = "unknown" | "ok" | "insecure_context" | "unsupported";

export interface SpeechSession {
  state: SpeechState;
  read: StatusRead;
  availability: Availability;
  mic: MicSupport;
  /** The microphone is open (recording, or kept for a few seconds). */
  micOpen: boolean;
  /** The ref of the button that talks. */
  attach: (button: HTMLElement | null) => void;
  /** The ref of the level bar; the session sets `--rg-level` on it (0..1). */
  meter: (bar: HTMLElement | null) => void;
  use: (prompt: string) => void;
  pick: (prompt: string) => void;
  showAll: () => void;
  edit: (on: boolean) => void;
  draft: (text: string) => void;
  /** Matches the edited text against the allowed list again. */
  rematch: (text: string) => void;
  recheck: () => void;
  release: () => void;
}

export interface SpeechSessionOptions {
  api: SpeechApi;
  /** The model of the form: it decides which sentences are allowed. */
  modelId: string;
  /** Called with a sentence of the allowed list, once per press of Use. */
  onUse: (prompt: string) => void;
  /** Replaces the microphone capture (tests). */
  createCapture?: (events: CaptureEvents) => CaptureLike;
}

const defaultCapture = (events: CaptureEvents): CaptureLike =>
  new VoiceCapture(events);

export function useSpeechSession({
  api,
  modelId,
  onUse,
  createCapture = defaultCapture,
}: SpeechSessionOptions): SpeechSession {
  const [state, rawDispatch] = useReducer(speechReducer, initialSpeech);
  const [read, setRead] = useState<StatusRead>({ kind: "loading" });
  const [mic, setMic] = useState<MicSupport>("unknown");
  const [micOpen, setMicOpen] = useState(false);

  // The latest values for callbacks that outlive a render. An action moves the
  // state in the ref at once too, so two calls in one event (a double press of
  // Use) see the state the first one left.
  const stateRef = useRef(state);
  stateRef.current = state;
  const dispatch = useCallback((action: SpeechAction) => {
    stateRef.current = speechReducer(stateRef.current, action);
    rawDispatch(action);
  }, []);
  const modelRef = useRef(modelId);
  modelRef.current = modelId;
  const makeCapture = useRef(createCapture);
  makeCapture.current = createCapture;
  const onUseRef = useRef(onUse);
  onUseRef.current = onUse;
  const usable = availability(read).usable && mic === "ok";
  const usableRef = useRef(usable);
  usableRef.current = usable;

  const alive = useRef(false);
  const seq = useRef(0);
  const inflight = useRef<AbortController | null>(null);
  const pendingUse = useRef<AbortController | null>(null);
  const statusCall = useRef<AbortController | null>(null);
  const statusAt = useRef(0);
  const capture = useRef<CaptureLike | null>(null);
  const detach = useRef<(() => void) | null>(null);

  // -- the voice state

  const recheck = useCallback(() => {
    statusCall.current?.abort();
    const controller = new AbortController();
    statusCall.current = controller;
    api
      .status(controller.signal, modelRef.current)
      .then((answer) => {
        if (controller.signal.aborted || !alive.current) return;
        statusAt.current = Date.now();
        if (!answer.ok) return setRead({ kind: "problem", failure: answer });
        const status = readSpeechStatus(answer.data);
        setRead(
          status ? { kind: "ok", status } : { kind: "problem", failure: null },
        );
      })
      .catch(() => undefined); // aborted: a newer read, or the page left
  }, [api]);

  useEffect(() => {
    alive.current = true;
    setMic(micSupport());
    recheck();
    const onVisible = () => {
      if (document.visibilityState === "hidden") return;
      if (Date.now() - statusAt.current > STATUS_STALE_MS) recheck();
    };
    const refresh = setInterval(() => {
      if (document.visibilityState !== "hidden") recheck();
    }, STATUS_STALE_MS);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      alive.current = false;
      clearInterval(refresh);
      document.removeEventListener("visibilitychange", onVisible);
      inflight.current?.abort();
      statusCall.current?.abort();
    };
  }, [recheck]);

  // -- requests

  const problemOf = (failure: ApiFailure) =>
    ({ kind: "api", failure }) as const;

  /** Matches `transcript` against the allowed list of the form's model. */
  const check = useCallback(
    (transcript: string, edited: boolean) => {
      inflight.current?.abort();
      const controller = new AbortController();
      inflight.current = controller;
      seq.current += 1;
      const mine = seq.current;
      const forModel = modelRef.current;
      dispatch({ type: "check", seq: mine });
      api
        .match({ transcript, modelId: forModel }, controller.signal)
        .then((answer) => {
          if (controller.signal.aborted || !alive.current) return;
          if (!answer.ok)
            return dispatch({
              type: "failed",
              seq: mine,
              problem: problemOf(answer),
            });
          const match = readMatched(answer.data);
          if (!match)
            return dispatch({
              type: "failed",
              seq: mine,
              problem: { kind: "format" },
            });
          // The model was switched while this was on its way.
          if (modelRef.current !== forModel) return check(transcript, edited);
          dispatch({
            type: "checked",
            seq: mine,
            at: Date.now(),
            transcript,
            match,
            forModel,
            edited,
          });
        })
        .catch(() => undefined);
    },
    [api, dispatch],
  );

  const upload = useCallback(
    (clip: Clip) => {
      inflight.current?.abort();
      const controller = new AbortController();
      inflight.current = controller;
      const mine = seq.current;
      const forModel = modelRef.current;
      const requestId = newRequestId();
      dispatch({ type: "uploading", seq: mine });
      api
        .transcribe(
          clip.blob,
          { requestId, modelId: forModel },
          controller.signal,
        )
        .then((answer) => {
          if (controller.signal.aborted || !alive.current) return;
          // The voice state may have changed with this request (the model is
          // loaded now, or the worker failed).
          recheck();
          if (!answer.ok)
            return dispatch({
              type: "failed",
              seq: mine,
              problem: problemOf(answer),
            });
          const heard = readHeard(answer.data, requestId);
          if (!heard)
            return dispatch({
              type: "failed",
              seq: mine,
              problem: { kind: "format" },
            });
          dispatch({
            type: "answer",
            seq: mine,
            at: Date.now(),
            heard,
            forModel,
            truncated: clip.truncated,
          });
          // The model was switched while the clip was on its way: its match
          // belongs to the old model's list.
          if (heard.heard && modelRef.current !== forModel)
            check(heard.transcript, false);
        })
        .catch(() => undefined);
    },
    [api, check, dispatch, recheck],
  );

  // -- the button

  const attach = useCallback(
    (button: HTMLElement | null) => {
      detach.current?.();
      detach.current = null;
      capture.current?.dispose();
      capture.current = null;
      setMicOpen(false);
      if (!button) return;
      const cap = makeCapture.current({
        onState: (next, prev) => {
          // A new recording starts from idle: it is the newest request.
          if (prev === "idle") {
            inflight.current?.abort();
            seq.current += 1;
            dispatch({ type: "begin", seq: seq.current });
          }
          if (next === "recording") dispatch({ type: "recording" });
          // Idle without a clip (cancelled, aborted, or an error that follows).
          if (next === "idle" && prev !== "encoding" && prev !== "idle")
            dispatch({ type: "stopped" });
        },
        onLevel: (level) => paint(dbToFraction(level.peakDb)),
        onClip: upload,
        onError: ({ code }) => dispatch({ type: "mic_problem", code }),
        onMic: (open) => {
          if (alive.current) setMicOpen(open);
        },
      });
      capture.current = cap;
      // The state of the voice input decides, at the moment of the press,
      // whether a capture may start at all.
      const guarded: CaptureLike = {
        get state() {
          return cap.state;
        },
        start: () =>
          usableRef.current &&
          stateRef.current.phase !== "uploading" &&
          !stateRef.current.proposal?.checking
            ? cap.start()
            : Promise.resolve(false),
        stop: () => cap.stop(),
        cancel: () => cap.cancel(),
        abort: () => cap.abort(),
        dispose: () => cap.dispose(),
        addStateListener: (listener) => cap.addStateListener(listener),
      };
      detach.current = attachPushToTalk(button, guarded);
    },
    // `paint` is stable (refs only).
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [createCapture, upload],
  );

  // The page left: the microphone goes with it.
  useEffect(
    () => () => {
      detach.current?.();
      detach.current = null;
      capture.current?.dispose();
      capture.current = null;
    },
    [],
  );

  // -- the level bar, painted outside React's render (about 90 updates a second)

  const bar = useRef<HTMLElement | null>(null);
  const level = useRef(0);
  const frame = useRef<number | null>(null);
  function paint(fraction: number) {
    level.current = fraction;
    if (frame.current !== null || !bar.current) return;
    frame.current = requestAnimationFrame(() => {
      frame.current = null;
      bar.current?.style.setProperty("--rg-level", level.current.toFixed(3));
    });
  }
  const meter = useCallback((element: HTMLElement | null) => {
    bar.current = element;
  }, []);
  useEffect(() => {
    if (state.phase !== "recording")
      bar.current?.style.setProperty("--rg-level", "0");
  }, [state.phase]);
  useEffect(
    () => () => {
      if (frame.current !== null) cancelAnimationFrame(frame.current);
    },
    [],
  );

  // -- the proposal's clock

  const proposalId = state.proposal?.id ?? null;
  const shownAt = state.proposal?.shownAt ?? 0;
  useEffect(() => {
    if (proposalId === null) return;
    const now = Date.now();
    const arm = setTimeout(
      () => dispatch({ type: "arm", id: proposalId }),
      Math.max(0, shownAt + USE_DELAY_MS - now),
    );
    const expire = setTimeout(
      () => dispatch({ type: "expire", id: proposalId }),
      Math.max(0, shownAt + PROPOSAL_TTL_MS - now),
    );
    return () => {
      clearTimeout(arm);
      clearTimeout(expire);
    };
  }, [dispatch, proposalId, shownAt]);

  // -- a model switch revalidates what is on offer

  const lastModel = useRef(modelId);
  useEffect(() => {
    if (lastModel.current === modelId) return;
    lastModel.current = modelId;
    setRead({ kind: "loading" });
    usableRef.current = false;
    recheck();
    const { proposal, phase } = stateRef.current;
    if (proposal && phase === "proposal")
      check(proposal.transcript, proposal.edited);
  }, [modelId, check, recheck]);

  // -- what the person can do

  const use = useCallback(
    (prompt: string) => {
      if (!canUse(stateRef.current, prompt, Date.now())) return;
      const proposal = stateRef.current.proposal!;
      const forModel = modelRef.current;
      if (proposal.forModel !== forModel) return;
      // Revalidate the exact allowed list immediately before filling the form.
      // Checking also fences double presses and a second capture.
      inflight.current?.abort();
      const controller = new AbortController();
      inflight.current = controller;
      pendingUse.current = controller;
      dispatch({ type: "check", seq: stateRef.current.seq });
      api
        .status(controller.signal, forModel)
        .then((answer) => {
          if (
            controller.signal.aborted ||
            pendingUse.current !== controller ||
            !alive.current ||
            modelRef.current !== forModel ||
            stateRef.current.proposal?.id !== proposal.id
          )
            return;
          const status = answer.ok ? readSpeechStatus(answer.data) : null;
          if (
            !status ||
            !availability({ kind: "ok", status }).usable ||
            !proposal.match.revision ||
            status.allowedRevision !== proposal.match.revision ||
            Date.now() - proposal.shownAt >= PROPOSAL_TTL_MS
          ) {
            dispatch({ type: "expire", id: proposal.id });
            recheck();
            return;
          }
          dispatch({ type: "used" });
          onUseRef.current(prompt);
        })
        .catch(() => {
          if (!controller.signal.aborted && alive.current)
            dispatch({ type: "expire", id: proposal.id });
        })
        .finally(() => {
          if (pendingUse.current === controller) pendingUse.current = null;
        });
    },
    [api, dispatch, recheck],
  );
  const rematch = useCallback(
    (text: string) => {
      const proposal = stateRef.current.proposal;
      const typed = text.trim();
      if (!proposal || !typed) return;
      if (typed === proposal.transcript)
        return dispatch({ type: "edit", on: false });
      check(typed, true);
    },
    [check, dispatch],
  );
  const release = useCallback(() => {
    api
      .release()
      .then(() => alive.current && recheck())
      .catch(() => undefined);
  }, [api, recheck]);

  return useMemo(
    () => ({
      state,
      read,
      availability: availability(read),
      mic,
      micOpen,
      attach,
      meter,
      use,
      pick: (prompt) => dispatch({ type: "pick", prompt }),
      showAll: () => {
        const pending = pendingUse.current;
        const proposal = stateRef.current.proposal;
        if (
          pending &&
          pending === inflight.current &&
          !pending.signal.aborted
        ) {
          // Rejecting the candidate also revokes the pending Use, even if its
          // status response arrives later with the same proposal/model IDs.
          pending.abort();
          pendingUse.current = null;
          if (proposal) dispatch({ type: "cancel_use", id: proposal.id });
        }
        dispatch({ type: "show_all" });
      },
      edit: (on) => dispatch({ type: "edit", on }),
      draft: (text) => dispatch({ type: "draft", text }),
      rematch,
      recheck,
      release,
    }),
    [
      state,
      read,
      mic,
      micOpen,
      attach,
      meter,
      use,
      rematch,
      recheck,
      release,
      dispatch,
    ],
  );
}
