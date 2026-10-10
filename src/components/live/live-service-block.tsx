"use client";
// "Background service" block of the live page: whether the live service
// runs, who started it, which ports and GPU facts decide a start, and the
// Start / Stop buttons behind their confirmations. The page only asks the
// core (`GET/POST /api/levi/live/service*`, levi/live/service_control.py);
// the core carries out nothing unless LEVI_LIVE_SERVICE_CONTROL=1, and a
// person's token is added by the same-origin proxy, never seen here.
import { useCallback, useEffect, useRef, useState } from "react";
import { useLocale } from "@/components/levi-locale";
import { Badge, Button, Checkbox, Field, Input } from "@/components/ds";
import { Note, Problem } from "@/components/pages-ui/feedback";
import type { Tone as DsTone } from "@/components/ds";
import { Field as Row } from "./session-panels";
import { CopyCommand, START_COMMAND } from "./service-panel";
import {
  serviceApi,
  type ApiResult,
  type ServiceApi,
} from "./live-service-api";
import {
  CODE_TEXT,
  CONTROL_SETTING,
  FORCE_PHRASE,
  KIND_TEXT,
  OPERATION_TEXT,
  PORT_TEXT,
  ROLE_TEXT,
  START_PHRASE,
  STOP_PHRASE,
  RequestIds,
  codeKey,
  operationOver,
  serviceKind,
  startBlock,
  startBody,
  startConfirmations,
  startRefusals,
  startedBy,
  stopBlock,
  stopBody,
  stopNeedsForce,
  stopRefusals,
  type ServiceCheck,
  type ServiceKind,
  type ServiceOperation,
  type ServiceStatus,
} from "./live-service-logic";

const KIND_TONE: Record<ServiceKind, DsTone> = {
  running: "success",
  starting: "warning",
  stopped: "neutral",
  failed: "danger",
};
const PORT_TONE: Record<string, DsTone> = {
  free: "neutral",
  ours: "success",
  foreign: "danger",
  unknown: "warning",
};
const OP_TONE: Record<string, DsTone> = {
  preparing: "info",
  running: "info",
  done: "success",
  failed: "danger",
  refused: "warning",
};

const IDLE_MS = 10_000;
const BUSY_MS = 2_000;
const OP_MS = 1_000;

interface ProblemInfo {
  code: string;
  message: string;
}

/** One refusal or confirmation, in the reader's language when the code is
 * known, with the core's own sentence (which carries the specifics: port,
 * pid, names) after it. */
function CheckLine({ check }: { check: ServiceCheck | ProblemInfo }) {
  const { t } = useLocale();
  const key = codeKey(check.code);
  const message = "message" in check ? check.message : "";
  return (
    <li>
      {key ? t(key) : message}
      {key && message && (
        <span className="pg-pool-muted">
          {" "}
          <code>{message}</code>
        </span>
      )}
    </li>
  );
}

export function LiveServiceBlock({ api = serviceApi }: { api?: ServiceApi }) {
  const { t } = useLocale();
  const [status, setStatus] = useState<ServiceStatus | null>(null);
  const [loadError, setLoadError] = useState("");
  const [op, setOp] = useState<ServiceOperation | null>(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<ProblemInfo | null>(null);
  // The core said 403 `disabled` to a press: treat the block as read-only
  // even if the last status still said enabled.
  const [refusedDisabled, setRefusedDisabled] = useState(false);
  const [gpuShared, setGpuShared] = useState(false);
  const [startPhrase, setStartPhrase] = useState("");
  const [stopOpen, setStopOpen] = useState(false);
  const [stopPhrase, setStopPhrase] = useState("");
  const [forcePhrase, setForcePhrase] = useState("");
  const busyRef = useRef(false);
  const ids = useRef(new RequestIds());
  const opId = op && !operationOver(op) ? op.operation_id : null;
  const serverOp = status?.operation;
  const watching =
    opId ??
    (serverOp && !operationOver(serverOp) ? serverOp.operation_id : null);

  const refresh = useCallback(
    async (signal?: AbortSignal) => {
      try {
        const result = await api.status(signal);
        if (signal?.aborted) return;
        if (result.ok) {
          setStatus(result.data);
          setLoadError("");
        } else setLoadError(result.message);
      } catch {
        // aborted on unmount
      }
    },
    [api],
  );

  // Status: every 10 s, every 2 s while a start or stop is in progress;
  // nothing while the tab is hidden.
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      if (document.visibilityState !== "hidden")
        await refresh(controller.signal);
      if (!controller.signal.aborted)
        timer = setTimeout(tick, watching ? BUSY_MS : IDLE_MS);
    };
    void tick();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [refresh, watching]);

  // The operation: asked every second until it is done, failed or refused.
  useEffect(() => {
    if (!opId) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      try {
        const result = await api.operation(opId, controller.signal);
        if (controller.signal.aborted) return;
        if (result.ok) {
          setOp(result.data);
          if (operationOver(result.data)) {
            ids.current.release();
            void refresh();
            return;
          }
        } else if (result.status === 404) {
          // The core forgot it (kept in memory only): the status tells the rest.
          setOp(null);
          ids.current.release();
          void refresh();
          return;
        }
      } catch {
        return;
      }
      timer = setTimeout(tick, OP_MS);
    };
    timer = setTimeout(tick, OP_MS);
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [opId, api, refresh]);

  const submit = async (action: "start" | "stop") => {
    if (busyRef.current || !status) return;
    busyRef.current = true;
    setBusy(true);
    setProblem(null);
    const id = ids.current.next(action);
    let result: ApiResult<ServiceOperation>;
    try {
      result =
        action === "start"
          ? await api.start(
              startBody(status, { gpuShared, phrase: startPhrase }, id),
            )
          : await api.stop(stopBody(status, forcePhrase, id));
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
    if (result.ok) {
      setOp(result.data);
      setStopOpen(false);
      setStopPhrase("");
      setForcePhrase("");
      setStartPhrase("");
      setGpuShared(false);
      return;
    }
    // A refusal is final for this request id (the core gives it again to the
    // same id): the next press is a new request.
    ids.current.release();
    if (result.code === "busy" && result.operationId) {
      setOp({
        operation_id: result.operationId,
        action: "?",
        state: "running",
      });
    } else if (result.code === "disabled") setRefusedDisabled(true);
    else {
      if (result.code === "evaluation_active") setStopOpen(true);
      setProblem({ code: result.code, message: result.message });
    }
    void refresh();
  };

  const heading = <h2 id="live-service-control">{t("liveService.title")}</h2>;
  if (!status) {
    return (
      <section
        className="pg-live-section"
        aria-labelledby="live-service-control"
      >
        {heading}
        <p className="pg-pool-muted" role="status">
          {loadError
            ? `${t("liveService.loadFailed")}: ${loadError}`
            : t("liveService.loading")}
        </p>
        <CopyCommand command={START_COMMAND} />
      </section>
    );
  }

  const kind = serviceKind(status);
  const by = startedBy(status);
  const enabled = status.enabled && !refusedDisabled;
  const control = enabled && !status.served_by_live_service;
  const running = kind === "running" || kind === "starting";
  const startRef = startRefusals(status);
  const stopRef = stopRefusals(status);
  const confirms = startConfirmations(status);
  const needsForce = stopNeedsForce(status);
  const form = { gpuShared, phrase: startPhrase };
  const startOff = startBlock({ ...status, enabled }, form, busy || !!opId);
  const stopOff = stopBlock(
    { ...status, enabled },
    stopPhrase,
    forcePhrase,
    busy || !!opId,
  );
  const startPhraseWord = status.confirm?.start ?? START_PHRASE;
  const stopPhraseWord = status.confirm?.stop ?? STOP_PHRASE;
  const forceWord = status.confirm?.force_phrase ?? FORCE_PHRASE;
  const others = status.gpu?.others ?? null;
  const shownOp =
    op ?? (serverOp && !operationOver(serverOp) ? serverOp : null);
  const failedUnit = confirms.includes("unit_failed");
  const sayPid = (key: string, pid?: number | null) =>
    t(key).replace("{pid}", String(pid ?? "?"));

  return (
    <section
      className="pg-live-section"
      aria-labelledby="live-service-control"
      data-kind={kind}
    >
      {heading}
      <div className="pg-live-chips">
        <Badge tone={KIND_TONE[kind]}>{t(KIND_TEXT[kind])}</Badge>
        {by === "unit" && (
          <Badge tone="neutral">{t("liveService.byUnit")}</Badge>
        )}
        {(by === "terminal" || by === "terminal_once") && (
          <Badge tone="neutral">
            {sayPid("liveService.byTerminal", status.holder?.pid)}
          </Badge>
        )}
        {by === "other" && (
          <Badge tone="neutral">
            {sayPid("liveService.byOther", status.holder?.pid)}
          </Badge>
        )}
      </div>
      {loadError && (
        <p className="pg-live-bad" role="status">
          {t("liveService.loadFailed")}: {loadError}
        </p>
      )}

      {!enabled && (
        <Note tone="info" role="status">
          <strong>{t("liveService.disabledTitle")}</strong>{" "}
          {t("liveService.disabledBody")} <code>{CONTROL_SETTING}</code>{" "}
          {t("liveService.disabledRestart")}
        </Note>
      )}
      {enabled && status.served_by_live_service && (
        <Note tone="info" role="status">
          {t("liveService.servedByLive")}
        </Note>
      )}

      <dl className="pg-live-dl">
        <Row label={t("liveService.unit")}>
          {status.unit?.installed ? (
            <>
              <code>{status.unit.name}</code>
              {status.unit.state && (
                <span className="pg-pool-muted">
                  {" "}
                  · {status.unit.state}
                  {status.unit.sub_state ? `/${status.unit.sub_state}` : ""}
                </span>
              )}
            </>
          ) : (
            t("liveService.unitMissing")
          )}
        </Row>
        <Row label={t("liveService.vllm")}>
          <code>{status.gpu?.vllm_state ?? "—"}</code>
        </Row>
        <Row label={t("liveService.gpuOthers")}>
          {others == null
            ? t("liveService.gpuUnknown")
            : others.length === 0
              ? t("liveService.gpuNone")
              : others
                  .slice(0, 4)
                  .map(
                    (p) =>
                      `${p.name ?? "?"} (pid ${p.pid}${
                        p.memory_mib != null
                          ? `, ${(p.memory_mib / 1024).toFixed(1)} GiB`
                          : ""
                      })`,
                  )
                  .join("; ")}
        </Row>
        <Row label={t("liveService.gpuLock")}>
          {status.gpu?.lock?.state === "other"
            ? sayPid("liveService.lockOther", status.gpu.lock.pid)
            : (status.gpu?.lock?.state ?? "—")}
        </Row>
        <Row label={t("liveService.evaluation")}>
          {status.evaluation?.active == null
            ? t("liveService.evalUnknown")
            : status.evaluation.active
              ? t("liveService.evalActive")
              : t("liveService.evalNone")}
        </Row>
      </dl>

      <h3 className="pg-live-sub">{t("liveService.ports")}</h3>
      <ul className="pg-live-ports">
        {(status.ports ?? []).map((row) => (
          <li key={row.port}>
            <code>{row.port}</code> {t(ROLE_TEXT[row.role] ?? row.role)}{" "}
            <Badge tone={PORT_TONE[row.state] ?? "neutral"}>
              {t(PORT_TEXT[row.state] ?? row.state)}
            </Badge>
            {row.state === "foreign" && row.pid != null && (
              <span className="pg-pool-muted">
                {" "}
                pid {row.pid}
                {row.name ? ` (${row.name})` : ""}
              </span>
            )}
          </li>
        ))}
      </ul>

      {(running ? stopRef : startRef).length > 0 && (
        <div className="pg-live-gate warn" role="status">
          <strong>
            {t(running ? "liveService.cannotStop" : "liveService.cannotStart")}
          </strong>
          <ul>
            {(running ? stopRef : startRef).map((check) => (
              <CheckLine key={`${check.code}-${check.message}`} check={check} />
            ))}
          </ul>
        </div>
      )}

      {shownOp && (
        <div className="pg-live-gate" role="status" aria-live="polite">
          <strong>
            <Badge tone={OP_TONE[shownOp.state] ?? "neutral"}>
              {t(OPERATION_TEXT[shownOp.state] ?? shownOp.state)}
            </Badge>{" "}
            {shownOp.action === "start"
              ? t("liveService.opStart")
              : shownOp.action === "stop"
                ? t("liveService.opStop")
                : t("liveService.opOther")}
          </strong>
          {shownOp.detail && (
            <p>
              <code>{shownOp.detail}</code>
            </p>
          )}
          {shownOp.result && shownOp.state !== "done" && (
            <p className="pg-pool-muted">
              <code>{shownOp.result}</code>
            </p>
          )}
        </div>
      )}
      {problem && (
        <Problem
          title={t("liveService.requestFailed")}
          why={
            codeKey(problem.code)
              ? `${t(codeKey(problem.code) as string)} (${problem.message})`
              : problem.message
          }
        />
      )}

      {control && !running && (
        <div className="pg-live-service-start">
          {confirms.includes("gpu_shared") && (
            <Checkbox
              checked={gpuShared}
              onChange={(event) => setGpuShared(event.target.checked)}
              label={t("liveService.gpuConfirm")}
              description={t("liveService.gpuConfirmHint")}
            />
          )}
          {failedUnit && (
            <p className="pg-pool-hint">{t("liveService.failedHint")}</p>
          )}
          <Field
            label={t("liveService.typeToStart")}
            hint={`${t("liveService.typePhrase")} ${startPhraseWord}`}
          >
            <Input
              value={startPhrase}
              onChange={(event) => setStartPhrase(event.target.value)}
              autoComplete="off"
              spellCheck={false}
            />
          </Field>
          <div className="pg-row">
            <Button
              variant="primary"
              loading={busy}
              disabled={startOff !== null}
              onClick={() => void submit("start")}
            >
              {t(
                failedUnit ? "liveService.resetAndStart" : "liveService.start",
              )}
            </Button>
            {startOff && startOff !== "liveService.block.busy" && (
              <span className="pg-pool-muted" role="status">
                {t(startOff)}
              </span>
            )}
          </div>
        </div>
      )}

      {control && running && (
        <div className="pg-live-service-stop">
          {!stopOpen ? (
            <Button
              variant="danger"
              disabled={busy || !!opId}
              onClick={() => setStopOpen(true)}
            >
              {t("liveService.stop")}
            </Button>
          ) : (
            <>
              {needsForce && (
                <Note tone="warning" role="alert">
                  {t("liveService.evalWarning")}
                </Note>
              )}
              <Field
                label={t("liveService.typeToStop")}
                hint={`${t("liveService.typePhrase")} ${stopPhraseWord}`}
              >
                <Input
                  value={stopPhrase}
                  onChange={(event) => setStopPhrase(event.target.value)}
                  autoComplete="off"
                  spellCheck={false}
                />
              </Field>
              {needsForce && (
                <Field
                  label={t("liveService.typeToForce")}
                  hint={`${t("liveService.typePhrase")} ${forceWord}`}
                >
                  <Input
                    value={forcePhrase}
                    onChange={(event) => setForcePhrase(event.target.value)}
                    autoComplete="off"
                    spellCheck={false}
                  />
                </Field>
              )}
              <div className="pg-row">
                <Button
                  variant="danger"
                  loading={busy}
                  disabled={stopOff !== null}
                  onClick={() => void submit("stop")}
                >
                  {t(
                    needsForce
                      ? "liveService.forceStop"
                      : "liveService.confirmStop",
                  )}
                </Button>
                <Button
                  variant="ghost"
                  disabled={busy}
                  onClick={() => {
                    setStopOpen(false);
                    setStopPhrase("");
                    setForcePhrase("");
                  }}
                >
                  {t("liveService.cancel")}
                </Button>
                {stopOff && stopOff !== "liveService.block.busy" && (
                  <span className="pg-pool-muted" role="status">
                    {t(stopOff)}
                  </span>
                )}
              </div>
            </>
          )}
        </div>
      )}

      {status.log_tail && status.log_tail.length > 0 && (
        <details className="pg-live-events">
          <summary>{t("liveService.log")}</summary>
          <pre className="pg-live-command">{status.log_tail.join("\n")}</pre>
        </details>
      )}

      {!running && (
        <details className="pg-live-events">
          <summary>{t("liveService.terminal")}</summary>
          <p className="pg-pool-muted">{t("liveService.terminalHint")}</p>
          <CopyCommand command={START_COMMAND} />
        </details>
      )}
    </section>
  );
}

// Every key the block asks for through a table (the catalogue test sees only
// literal t("…") arguments): kept here so a test can check them all.
export const SERVICE_TABLE_KEYS: string[] = [
  ...Object.values(CODE_TEXT),
  ...Object.values(KIND_TEXT),
  ...Object.values(OPERATION_TEXT),
  ...Object.values(PORT_TEXT),
  ...Object.values(ROLE_TEXT),
  "liveService.block.busy",
  "liveService.block.disabled",
  "liveService.block.servedByLive",
  "liveService.block.refused",
  "liveService.block.needGpuConfirm",
  "liveService.block.needPhrase",
  "liveService.block.needForcePhrase",
];
