"use client";
import { useEffect, useId, useState } from "react";
import { Button } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import {
  cloneWorkspaceBox,
  isValidWorkspaceBox,
  parseWorkspaceFields,
  pointInWorkspace,
  workspaceFields,
  type WorkspaceBox,
  type WorkspaceFields,
} from "./workspace-draft";

export interface WorkspacePanelProps {
  value: WorkspaceBox | null;
  onChange: (box: WorkspaceBox | null) => void;
  targetPosition: [number, number, number] | null;
  disabled?: boolean;
}

export default function WorkspacePanel({
  value,
  onChange,
  targetPosition,
  disabled = false,
}: WorkspacePanelProps) {
  const { t } = useLocale();
  const id = useId();
  const incomingValid = value === null || isValidWorkspaceBox(value);
  const [fields, setFields] = useState<WorkspaceFields>(() =>
    workspaceFields(incomingValid ? value : null),
  );
  const [invalidFields, setInvalidFields] = useState(false);
  useEffect(() => {
    setFields(
      workspaceFields(
        value === null || isValidWorkspaceBox(value) ? value : null,
      ),
    );
    setInvalidFields(value !== null && !isValidWorkspaceBox(value));
  }, [value]);

  let status = "robotStudio.workspace.unset";
  try {
    const inside = targetPosition
      ? pointInWorkspace(value, targetPosition)
      : null;
    if (!targetPosition) status = "robotStudio.workspace.unknownTarget";
    if (inside !== null)
      status = inside
        ? "robotStudio.workspace.inside"
        : "robotStudio.workspace.outside";
  } catch {
    status = "robotStudio.workspace.invalidTarget";
  }

  const apply = () => {
    if (disabled) return;
    let box: WorkspaceBox | null;
    try {
      box = parseWorkspaceFields(fields);
    } catch {
      setInvalidFields(true);
      return;
    }
    setInvalidFields(false);
    onChange(cloneWorkspaceBox(box));
  };

  return (
    <section
      className="rs-panel-section rs-workspace"
      aria-labelledby={`${id}-title`}
    >
      <h2 id={`${id}-title`}>{t("robotStudio.workspace.title")}</h2>
      <p className="rs-meta">{t("robotStudio.workspace.frame")}</p>
      <p
        className="rs-workspace-status"
        data-status={status.split(".").at(-1)}
        role="status"
        aria-live="polite"
      >
        {t(status)}
      </p>
      <p className="rs-meta">{t("robotStudio.workspace.pointOnly")}</p>
      <details className="rs-workspace-editor">
        <summary className="ds-focus">
          {t("robotStudio.workspace.edit")}
        </summary>
        <p className="rs-meta">{t("robotStudio.workspace.inputRange")}</p>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            apply();
          }}
          noValidate
        >
          <fieldset
            disabled={disabled}
            aria-describedby={invalidFields ? `${id}-error` : undefined}
          >
            <legend>{t("robotStudio.workspace.minimum")}</legend>
            <div className="rs-fields">
              {[0, 1, 2].map((axis) => (
                <label key={axis}>
                  <span>
                    {t(
                      [
                        "robotStudio.workspace.x",
                        "robotStudio.workspace.y",
                        "robotStudio.workspace.z",
                      ][axis],
                    )}
                    <small>mm</small>
                  </span>
                  <input
                    className="ds-input ds-focus"
                    aria-label={`${t("robotStudio.workspace.minimum")} ${t(["robotStudio.workspace.x", "robotStudio.workspace.y", "robotStudio.workspace.z"][axis])} (mm)`}
                    type="number"
                    step="any"
                    min="-2000"
                    max="2000"
                    value={fields[axis]}
                    aria-invalid={invalidFields || undefined}
                    onChange={(event) => {
                      setFields(
                        (old) =>
                          old.map((value, index) =>
                            index === axis ? event.target.value : value,
                          ) as WorkspaceFields,
                      );
                      setInvalidFields(false);
                    }}
                  />
                </label>
              ))}
            </div>
          </fieldset>
          <fieldset
            disabled={disabled}
            aria-describedby={invalidFields ? `${id}-error` : undefined}
          >
            <legend>{t("robotStudio.workspace.maximum")}</legend>
            <div className="rs-fields">
              {[0, 1, 2].map((axis) => (
                <label key={axis}>
                  <span>
                    {t(
                      [
                        "robotStudio.workspace.x",
                        "robotStudio.workspace.y",
                        "robotStudio.workspace.z",
                      ][axis],
                    )}
                    <small>mm</small>
                  </span>
                  <input
                    className="ds-input ds-focus"
                    aria-label={`${t("robotStudio.workspace.maximum")} ${t(["robotStudio.workspace.x", "robotStudio.workspace.y", "robotStudio.workspace.z"][axis])} (mm)`}
                    type="number"
                    step="any"
                    min="-2000"
                    max="2000"
                    value={fields[axis + 3]}
                    aria-invalid={invalidFields || undefined}
                    onChange={(event) => {
                      setFields(
                        (old) =>
                          old.map((value, index) =>
                            index === axis + 3 ? event.target.value : value,
                          ) as WorkspaceFields,
                      );
                      setInvalidFields(false);
                    }}
                  />
                </label>
              ))}
            </div>
          </fieldset>
          {invalidFields && (
            <p id={`${id}-error`} role="alert" className="rs-workspace-error">
              {t("robotStudio.workspace.invalidFields")}
            </p>
          )}
          <div className="rs-workspace-actions">
            <Button type="submit" size="sm" disabled={disabled}>
              {t("robotStudio.workspace.apply")}
            </Button>
            <Button
              type="button"
              size="sm"
              variant="ghost"
              disabled={disabled}
              onClick={() => {
                setFields(workspaceFields(null));
                setInvalidFields(false);
                onChange(null);
              }}
            >
              {t("robotStudio.workspace.clear")}
            </Button>
          </div>
        </form>
      </details>
    </section>
  );
}
