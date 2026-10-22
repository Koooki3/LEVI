"use client";
import { useState } from "react";
import { Download, SlidersHorizontal } from "lucide-react";
import { Button } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import {
  PARAMETER_CAPABILITIES,
  PARAMETER_FIELDS,
  emptyParameterFields,
  exportParameterDraft,
  parseParameterFields,
} from "./parameter-draft";

function downloadProposal(content: string) {
  const url = URL.createObjectURL(
    new Blob([content], { type: "application/json" }),
  );
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = "fr3-parameter-proposal.json";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export default function ParameterPanel({
  onExport = downloadProposal,
}: {
  onExport?: (content: string) => void;
}) {
  const { t } = useLocale();
  const [fields, setFields] = useState(emptyParameterFields);
  const parsed = parseParameterFields(fields);
  const hasDraft = Object.values(parsed.draft).some((value) => value !== null);
  const hasInput = Object.values(fields).some((values) =>
    values.some((value) => value !== ""),
  );
  const text = (key: string) => t("robotStudio.parameters." + key);
  return (
    <section
      className="rs-panel-section rs-parameter-panel"
      aria-labelledby="rs-parameter-title"
    >
      <h2 id="rs-parameter-title">
        <SlidersHorizontal size={16} aria-hidden="true" />
        {text("title")}
      </h2>
      <p className="rs-meta">{text("intro")}</p>
      <div className="rs-parameter-readback" role="status">
        <span>{text("current")}</span>
        <strong>{text("unknown")}</strong>
      </div>
      <details>
        <summary>{text("capabilities")}</summary>
        <ul className="rs-capability-list">
          {PARAMETER_CAPABILITIES.map(({ id, mode }) => (
            <li key={id} data-capability={id}>
              <div className="rs-capability-heading">
                <strong>{text(id + ".title")}</strong>
                <span className="rs-capability-state" data-mode={mode}>
                  {text("mode." + mode)}
                </span>
              </div>
              <p>{text(id + ".reason")}</p>
            </li>
          ))}
        </ul>
      </details>
      <details>
        <summary>{text("draft")}</summary>
        <p className="rs-meta">{text("draftHint")}</p>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (!parsed.invalid.length && hasDraft)
              onExport(exportParameterDraft(fields));
          }}
        >
          {PARAMETER_FIELDS.map(({ key, label, count, unit }) => (
            <fieldset key={key}>
              <legend>
                {text(label + ".label")} <small>{unit}</small>
              </legend>
              <div
                className={
                  count === 7 ? "rs-parameter-joints" : "rs-parameter-scalar"
                }
              >
                {fields[key].map((value, index) => (
                  <label key={index}>
                    <span>
                      {count === 7 ? `J${index + 1}` : text("proposal")}
                    </span>
                    <input
                      className="ds-input ds-focus"
                      type="number"
                      step="any"
                      min="0"
                      value={value}
                      aria-label={`${text(label + ".label")}${count === 7 ? ` J${index + 1}` : ""} (${unit})`}
                      aria-invalid={parsed.invalid.includes(key)}
                      aria-describedby={
                        parsed.invalid.includes(key)
                          ? `rs-parameter-error-${key}`
                          : undefined
                      }
                      onChange={(event) =>
                        setFields((old) => ({
                          ...old,
                          [key]: old[key].map((entry, i) =>
                            i === index ? event.target.value : entry,
                          ),
                        }))
                      }
                    />
                  </label>
                ))}
              </div>
              {parsed.invalid.includes(key) && (
                <p
                  className="rs-parameter-error"
                  id={`rs-parameter-error-${key}`}
                >
                  {text("invalid")}
                </p>
              )}
            </fieldset>
          ))}
          <div className="rs-actions">
            <Button
              type="submit"
              size="sm"
              icon={Download}
              disabled={!hasDraft || parsed.invalid.length > 0}
            >
              {text("export")}
            </Button>
            <Button
              type="button"
              size="sm"
              variant="ghost"
              disabled={!hasInput}
              onClick={() => setFields(emptyParameterFields())}
            >
              {text("clear")}
            </Button>
          </div>
          <p className="rs-meta">{text("exportHint")}</p>
        </form>
      </details>
    </section>
  );
}
