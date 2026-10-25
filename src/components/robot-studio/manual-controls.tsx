"use client";
import { useEffect, useMemo, useRef, useState } from "react";
import { Button } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { isValidWorkspaceBox, type WorkspaceBox } from "./workspace-draft";
import type { TeleopDraft } from "./tool-draft";
import {
  exportManualConfiguration,
  parseManualConfiguration,
  revealManualConfiguration,
} from "./manual-config";
import {
  manualApi,
  type ManualAnchor,
  type ManualApi,
  type ManualAttest,
  type ManualLimits,
  type ManualPoint,
  type ManualStopLimits,
} from "./manual-api";
import {
  ManualSessionController,
  manualStartAllowed,
  productionView,
  validManualLimits,
  type ManualDragController,
  type ManualSnapshot,
} from "./manual-session";

export type ManualControlsProps = {
  workspaceBox: WorkspaceBox | null;
  teleopDraft: TeleopDraft;
  targetPosition: ManualPoint;
  canDrag: boolean;
  independentGripper?: boolean;
  onConfigurationDraft?: (
    box: WorkspaceBox,
    teleop: Pick<TeleopDraft, "linearSpeedMps" | "positionStepMm">,
  ) => void;
  onSessionAnchor: (anchor: ManualAnchor | null) => void;
  onController: (controller: ManualDragController | null) => void;
  api?: ManualApi;
};

export const MANUAL_STOP_LIMIT_KEYS = [
  "settle_window_s",
  "settle_position_m",
  "settle_angle_rad",
  "gripper_settle_rad",
] as const;
export const MANUAL_EXTRA_LIMIT_KEYS = [
  "max_delta_m",
  "period_s",
  "max_reading_age_s",
  "max_anchor_age_s",
  "deadman_timeout_s",
  "target_timeout_s",
  "max_orientation_drift_rad",
  "quaternion_norm_tolerance",
  "max_settle_age_s",
] as const;
type ExtraFields = Record<(typeof MANUAL_EXTRA_LIMIT_KEYS)[number], string>;
const emptyFields = (): ExtraFields =>
  Object.fromEntries(
    MANUAL_EXTRA_LIMIT_KEYS.map((key) => [key, ""]),
  ) as ExtraFields;

/** Reuses one shared box/speed/step draft; blanks never become site defaults. */
export function manualLimitsFromDraft(
  box: WorkspaceBox | null,
  teleop: TeleopDraft,
  fields: ExtraFields,
): ManualLimits | null {
  if (
    !isValidWorkspaceBox(box) ||
    typeof teleop.linearSpeedMps !== "number" ||
    !Number.isFinite(teleop.linearSpeedMps) ||
    teleop.linearSpeedMps <= 0 ||
    typeof teleop.positionStepMm !== "number" ||
    !Number.isFinite(teleop.positionStepMm) ||
    teleop.positionStepMm <= 0
  )
    return null;
  const extra = {} as Record<(typeof MANUAL_EXTRA_LIMIT_KEYS)[number], number>;
  for (const key of MANUAL_EXTRA_LIMIT_KEYS) {
    const text = fields[key].trim();
    const value = Number(text);
    if (!text || !Number.isFinite(value) || value <= 0) return null;
    extra[key] = value;
  }
  const limits: ManualLimits = {
    ...extra,
    box_min_m: [...box.min],
    box_max_m: [...box.max],
    max_step_m: teleop.positionStepMm / 1000,
    max_velocity_m_s: teleop.linearSpeedMps,
  };
  return validManualLimits(limits) ? limits : null;
}

export default function ManualControls({
  workspaceBox,
  teleopDraft,
  targetPosition,
  canDrag,
  independentGripper = false,
  onConfigurationDraft,
  onSessionAnchor,
  onController,
  api = manualApi,
}: ManualControlsProps) {
  const { t } = useLocale();
  const [fields, setFields] = useState<ExtraFields>(emptyFields);
  const [stopFields, setStopFields] = useState<Record<string, string>>({});
  const [gripperRatio, setGripperRatio] = useState("");
  const [confirm, setConfirm] = useState("");
  const [configurationText, setConfigurationText] = useState("");
  const [configurationError, setConfigurationError] = useState(false);
  const [attest, setAttest] = useState<ManualAttest>({
    estop: false,
    scene_reset: false,
    area_clear: false,
  });
  const instance = useRef<ManualSessionController | null>(null);
  const [snapshot, setSnapshot] = useState<ManualSnapshot>({
    phase: "unavailable",
    reason: "",
    view: null,
    anchor: null,
    confirm: null,
  });
  const callbacks = useRef({ onController, onSessionAnchor, canDrag });
  callbacks.current = { onController, onSessionAnchor, canDrag };
  const bridge = useMemo<ManualDragController>(
    () => ({
      beginDrag: () =>
        callbacks.current.canDrag && (instance.current?.beginDrag() ?? false),
      target: (position) => instance.current?.target(position),
      revoke: (reason) => instance.current?.revoke(reason),
    }),
    [],
  );
  const limits = manualLimitsFromDraft(workspaceBox, teleopDraft, fields);
  const real = productionView(snapshot.view);
  const stopLimits = Object.fromEntries(
    MANUAL_STOP_LIMIT_KEYS.map((key) => [key, Number(stopFields[key] || NaN)]),
  ) as ManualStopLimits;
  const validStop = MANUAL_STOP_LIMIT_KEYS.every(
    (key) => Number.isFinite(stopLimits[key]) && stopLimits[key] > 0,
  );
  const configKey = JSON.stringify({
    workspaceBox,
    speed: teleopDraft.linearSpeedMps,
    step: teleopDraft.positionStepMm,
    fields,
    stopFields,
  });
  const previousConfig = useRef(configKey);
  useEffect(() => {
    const controller = new ManualSessionController(api);
    instance.current = controller;
    const unsubscribe = controller.subscribe(setSnapshot);
    callbacks.current.onController(bridge);
    void controller.refresh();
    const blur = () => controller.revoke("blur");
    const hidden = () => {
      if (document.visibilityState === "hidden") controller.revoke("hidden");
    };
    // The real 3D onDragStart owns beginDrag. Capture-phase cancellation stops
    // before React updates or TransformControls can enqueue another target.
    const release = () => {
      if (controller.snapshot().phase === "holding")
        controller.revoke("release");
    };
    const cancel = () => {
      if (controller.snapshot().phase === "holding")
        controller.revoke("pointer_cancel");
    };
    const key = (event: KeyboardEvent) => {
      if (event.key === "Escape") controller.revoke("escape");
    };
    window.addEventListener("blur", blur);
    window.addEventListener("pointerup", release, true);
    window.addEventListener("pointercancel", cancel, true);
    window.addEventListener("lostpointercapture", cancel, true);
    window.addEventListener("keydown", key);
    document.addEventListener("visibilitychange", hidden);
    return () => {
      unsubscribe();
      callbacks.current.onController(null);
      callbacks.current.onSessionAnchor(null);
      window.removeEventListener("blur", blur);
      window.removeEventListener("pointerup", release, true);
      window.removeEventListener("pointercancel", cancel, true);
      window.removeEventListener("lostpointercapture", cancel, true);
      window.removeEventListener("keydown", key);
      document.removeEventListener("visibilitychange", hidden);
      controller.dispose();
      if (instance.current === controller) instance.current = null;
    };
  }, [api, bridge]);
  const activeAnchor =
    snapshot.phase === "armed" || snapshot.phase === "holding"
      ? snapshot.anchor
      : null;
  useEffect(() => {
    callbacks.current.onSessionAnchor(activeAnchor);
  }, [activeAnchor]);
  useEffect(() => {
    const controller = instance.current;
    if (
      controller &&
      !canDrag &&
      ["review", "starting", "armed", "holding"].includes(
        controller.snapshot().phase,
      )
    )
      controller.revoke("drag_disabled");
  }, [canDrag]);
  useEffect(() => {
    if (previousConfig.current !== configKey) {
      previousConfig.current = configKey;
      const controller = instance.current;
      if (
        controller &&
        ["review", "starting", "armed", "holding"].includes(
          controller.snapshot().phase,
        )
      )
        controller.revoke("configuration_changed");
    }
  }, [configKey]);

  const locked = ["review", "starting", "armed", "holding", "closing"].includes(
    snapshot.phase,
  );
  const allowed = manualStartAllowed(snapshot.view);
  const reason = snapshot.reason || snapshot.view?.reason_code;
  const canReview =
    snapshot.phase === "ready" &&
    allowed &&
    limits !== null &&
    canDrag &&
    (!real || validStop);
  const missing: { label: string; target: string }[] = [];
  if (!canDrag)
    missing.push({
      label: t("robotStudio.controls.editFirst"),
      target: "rs-edit-target",
    });
  if (!isValidWorkspaceBox(workspaceBox))
    missing.push({
      label: t("robotStudio.manual.sharedBox"),
      target: "rs-workspace-configuration",
    });
  if (
    !(
      typeof teleopDraft.linearSpeedMps === "number" &&
      teleopDraft.linearSpeedMps > 0
    )
  )
    missing.push({
      label: t("robotStudio.manual.sharedVelocity"),
      target: "rs-teleop-configuration",
    });
  if (
    !(
      typeof teleopDraft.positionStepMm === "number" &&
      teleopDraft.positionStepMm > 0
    )
  )
    missing.push({
      label: t("robotStudio.manual.sharedStep"),
      target: "rs-teleop-configuration",
    });
  for (const key of MANUAL_EXTRA_LIMIT_KEYS) {
    const value = Number(fields[key]);
    if (
      !fields[key].trim() ||
      !Number.isFinite(value) ||
      value <= 0 ||
      (key === "quaternion_norm_tolerance" && value >= 1) ||
      (key === "max_orientation_drift_rad" && value > Math.PI)
    )
      missing.push({
        label: t("robotStudio.manual." + key),
        target: "rs-manual-" + key,
      });
  }
  if (real)
    for (const key of MANUAL_STOP_LIMIT_KEYS) {
      if (!Number.isFinite(stopLimits[key]) || stopLimits[key] <= 0)
        missing.push({
          label: t("robotStudio.manual." + key),
          target: "rs-manual-" + key,
        });
    }
  const allAttested = attest.estop && attest.scene_reset && attest.area_clear;
  const evidence = snapshot.view?.stop_evidence;
  const stopReason =
    typeof evidence === "string"
      ? evidence
      : evidence &&
          typeof evidence === "object" &&
          "reason_code" in evidence &&
          typeof evidence.reason_code === "string"
        ? evidence.reason_code
        : "—";
  const counterValue = (key: string): number | string => {
    const value = snapshot.view?.counters[key];
    return typeof value === "number" && Number.isInteger(value) && value >= 0
      ? value
      : "—";
  };
  const boxText =
    workspaceBox && isValidWorkspaceBox(workspaceBox)
      ? workspaceBox.min.map((v) => (v * 1000).toFixed(1)).join(", ") +
        " → " +
        workspaceBox.max.map((v) => (v * 1000).toFixed(1)).join(", ") +
        " mm"
      : "—";

  return (
    <section
      className="rs-panel-section rs-manual-panel"
      data-phase={snapshot.phase}
    >
      <h2>{t("robotStudio.manual.title")}</h2>
      <p className="rs-meta">
        {t(
          snapshot.view?.environment === "synthetic"
            ? "robotStudio.manual.synthetic"
            : real
              ? "robotStudio.manual.production"
              : "robotStudio.manual.productionDisabled",
        )}
      </p>
      <p className="rs-manual-status" role="status" aria-live="polite">
        {t("robotStudio.manual.phase." + snapshot.phase)}
      </p>
      {missing.length > 0 && (
        <div
          className="rs-readiness"
          data-testid="manual-missing-configuration"
        >
          <h3>{t("robotStudio.controls.missing")}</h3>
          <ul>
            {missing.map(({ label, target }) => (
              <li key={target + label}>
                <button
                  type="button"
                  className="rs-link"
                  onClick={() => revealManualConfiguration(target)}
                >
                  {label}
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
      <details className="rs-configuration-transfer">
        <summary>{t("robotStudio.controls.configuration")}</summary>
        <p className="rs-meta">{t("robotStudio.controls.configurationHint")}</p>
        <label>
          <span>{t("robotStudio.controls.configurationJson")}</span>
          <textarea
            className="ds-input ds-focus"
            rows={5}
            value={configurationText}
            disabled={locked}
            maxLength={16384}
            onChange={(event) => {
              setConfigurationText(event.target.value);
              setConfigurationError(false);
            }}
          />
        </label>
        <div className="rs-actions">
          <Button
            size="sm"
            disabled={
              locked || !onConfigurationDraft || !configurationText.trim()
            }
            onClick={() => {
              if (locked || !onConfigurationDraft) return;
              const config = parseManualConfiguration(configurationText);
              if (!config) {
                setConfigurationError(true);
                return;
              }
              const next = emptyFields();
              for (const key of MANUAL_EXTRA_LIMIT_KEYS)
                next[key] = String(config.limits[key]);
              setFields(next);
              setStopFields(
                Object.fromEntries(
                  MANUAL_STOP_LIMIT_KEYS.map((key) => [
                    key,
                    String(config.stop_limits[key]),
                  ]),
                ),
              );
              setAttest({
                estop: false,
                scene_reset: false,
                area_clear: false,
              });
              setConfirm("");
              setConfigurationError(false);
              onConfigurationDraft(
                {
                  min: [...config.limits.box_min_m],
                  max: [...config.limits.box_max_m],
                },
                {
                  linearSpeedMps: config.limits.max_velocity_m_s,
                  positionStepMm: config.limits.max_step_m * 1000,
                },
              );
            }}
          >
            {t("robotStudio.controls.importConfiguration")}
          </Button>
          <Button
            size="sm"
            disabled={locked || !limits || !validStop}
            onClick={() => {
              if (locked || !limits || !validStop) return;
              const content = exportManualConfiguration(limits, stopLimits);
              setConfigurationText(content);
              const url = URL.createObjectURL(
                new Blob([content], { type: "application/json" }),
              );
              const link = document.createElement("a");
              link.href = url;
              link.download = "fr3-manual-configuration.json";
              document.body.appendChild(link);
              link.click();
              link.remove();
              setTimeout(() => URL.revokeObjectURL(url), 1000);
            }}
          >
            {t("robotStudio.controls.exportConfiguration")}
          </Button>
        </div>
        {configurationError && (
          <p className="rs-meta" role="alert">
            {t("robotStudio.controls.invalidConfiguration")}
          </p>
        )}
      </details>
      <details>
        <summary>{t("robotStudio.controls.diagnostics")}</summary>
        <h3>{t("robotStudio.manual.counters")}</h3>
        <dl className="rs-manual-facts rs-manual-counters">
          {(["accepted", "sent", "acknowledged", "settled"] as const).map(
            (key) => (
              <div key={key} data-counter={key}>
                <dt>{t("robotStudio.manual." + key)}</dt>
                <dd>{counterValue(key)}</dd>
              </div>
            ),
          )}
        </dl>
        <p className="rs-meta rs-manual-stop-evidence">
          {t("robotStudio.manual.stopEvidence")} <code>{stopReason}</code>
        </p>
      </details>
      {reason && (
        <p className="rs-meta">
          {t("robotStudio.manual.gate")}{" "}
          {reason === "stop_unproven" ? (
            t("robotStudio.manual.stopUnproven")
          ) : (
            <code>{reason}</code>
          )}
        </p>
      )}
      {!limits && (
        <p className="rs-meta">{t("robotStudio.manual.limitsRequired")}</p>
      )}
      {!canDrag && (
        <p className="rs-meta">{t("robotStudio.manual.dragRequired")}</p>
      )}
      <details id="rs-manual-limits">
        <summary>{t("robotStudio.manual.sharedDraft")}</summary>
        <dl className="rs-manual-facts">
          <div>
            <dt>{t("robotStudio.manual.sharedBox")}</dt>
            <dd>{boxText}</dd>
          </div>
          <div>
            <dt>{t("robotStudio.manual.sharedVelocity")}</dt>
            <dd>
              {teleopDraft.linearSpeedMps === null
                ? "—"
                : teleopDraft.linearSpeedMps + " m/s"}
            </dd>
          </div>
          <div>
            <dt>{t("robotStudio.manual.sharedStep")}</dt>
            <dd>
              {teleopDraft.positionStepMm === null
                ? "—"
                : teleopDraft.positionStepMm + " mm"}
            </dd>
          </div>
        </dl>
        <fieldset disabled={locked}>
          <legend>{t("robotStudio.manual.extraLimits")}</legend>
          <div className="rs-manual-fields">
            {MANUAL_EXTRA_LIMIT_KEYS.map((name) => (
              <label key={name} id={"rs-manual-" + name}>
                <span>{t("robotStudio.manual." + name)}</span>
                <input
                  className="ds-input ds-focus"
                  type="number"
                  min="0"
                  step="any"
                  aria-label={t("robotStudio.manual." + name)}
                  value={fields[name]}
                  onChange={(event) =>
                    setFields((old) => ({ ...old, [name]: event.target.value }))
                  }
                />
              </label>
            ))}
          </div>
        </fieldset>
      </details>
      {real && (
        <fieldset disabled={locked}>
          <legend>{t("robotStudio.manual.stopLimits")}</legend>
          {MANUAL_STOP_LIMIT_KEYS.map((name) => (
            <label key={name} id={"rs-manual-" + name}>
              <span>{t("robotStudio.manual." + name)}</span>
              <input
                className="ds-input ds-focus"
                type="number"
                min="0"
                step="any"
                aria-label={t("robotStudio.manual." + name)}
                value={stopFields[name] || ""}
                onChange={(event) =>
                  setStopFields((old) => ({
                    ...old,
                    [name]: event.target.value,
                  }))
                }
              />
            </label>
          ))}
        </fieldset>
      )}
      <p className="rs-meta">{t("robotStudio.manual.translationOnly")}</p>
      {!independentGripper &&
        real &&
        snapshot.view?.capabilities.gripper === true && (
          <fieldset>
            <legend>{t("robotStudio.manual.realGripper")}</legend>
            <p className="rs-meta">{t("robotStudio.manual.gripperHint")}</p>
            <input
              className="ds-input ds-focus"
              type="number"
              min="0"
              max="1"
              step="any"
              aria-label={t("robotStudio.manual.openingRatio")}
              value={gripperRatio}
              disabled={snapshot.phase !== "armed"}
              onChange={(event) => setGripperRatio(event.target.value)}
            />
            <Button
              size="sm"
              disabled={
                snapshot.phase !== "armed" ||
                !gripperRatio.trim() ||
                !Number.isFinite(Number(gripperRatio)) ||
                Number(gripperRatio) < 0 ||
                Number(gripperRatio) > 1
              }
              onPointerDown={(event) => {
                if (event.button === 0)
                  instance.current?.beginGripper(Number(gripperRatio));
              }}
            >
              {t("robotStudio.manual.holdGripper")}
            </Button>
          </fieldset>
        )}
      <div className="rs-manual-actions">
        <Button
          size="sm"
          disabled={!canReview}
          onClick={() => {
            setConfirm("");
            setAttest({ estop: false, scene_reset: false, area_clear: false });
            void instance.current?.review(limits, real ? stopLimits : null);
          }}
        >
          {t("robotStudio.manual.review")}
        </Button>
        <Button
          size="sm"
          variant="ghost"
          disabled={locked}
          onClick={() => void instance.current?.refresh()}
        >
          {t("robotStudio.manual.refresh")}
        </Button>
        <Button
          size="sm"
          disabled={
            (!locked && snapshot.phase !== "fault") ||
            snapshot.phase === "closing"
          }
          onClick={() => instance.current?.revoke("person_stop")}
        >
          {t("robotStudio.manual.stop")}
        </Button>
      </div>
      {snapshot.phase === "review" && (
        <div className="rs-manual-confirm">
          <p className="rs-meta">
            {t("robotStudio.manual.confirmPrompt")}{" "}
            <code>{snapshot.confirm}</code>
          </p>
          <label>
            <span>{t("robotStudio.manual.confirmInput")}</span>
            <input
              className="ds-input ds-focus"
              aria-label={t("robotStudio.manual.confirmInput")}
              value={confirm}
              onChange={(event) => setConfirm(event.target.value)}
            />
          </label>
          {(
            [
              ["estop", "estop"],
              ["scene_reset", "sceneReset"],
              ["area_clear", "areaClear"],
            ] as const
          ).map(([name, label]) => (
            <label key={name} className="rs-manual-attest">
              <input
                type="checkbox"
                checked={attest[name]}
                onChange={(event) =>
                  setAttest((old) => ({ ...old, [name]: event.target.checked }))
                }
              />
              <span>{t("robotStudio.manual." + label)}</span>
            </label>
          ))}
          <Button
            size="sm"
            disabled={!canDrag || confirm !== snapshot.confirm || !allAttested}
            onClick={() => void instance.current?.start(confirm, attest)}
          >
            {t(
              real
                ? "robotStudio.manual.startReal"
                : "robotStudio.manual.start",
            )}
          </Button>
        </div>
      )}
      {snapshot.anchor && (
        <div className="rs-manual-anchor">
          <h3>{t("robotStudio.manual.anchor")}</h3>
          <p className="rs-meta">{t("robotStudio.manual.anchorFrame")}</p>
          <p className="rs-meta">
            <code>{snapshot.view?.anchor_id}</code>
          </p>
          <output>
            {snapshot.anchor.joints_rad.map((q) => q.toFixed(4)).join(" · ")}{" "}
            rad
          </output>
        </div>
      )}
      {(snapshot.phase === "armed" || snapshot.phase === "holding") && (
        <p className="rs-meta">{t("robotStudio.manual.readyHint")}</p>
      )}
      <p className="rs-meta">
        {t("robotStudio.manual.status")}{" "}
        {targetPosition.map((v) => (v * 1000).toFixed(1)).join(" · ")} mm
      </p>
    </section>
  );
}
