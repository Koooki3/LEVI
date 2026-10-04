"use client";
import { useEffect, useState } from "react";
import { T, useLocale } from "@/components/levi-locale";
import { leviApi } from "@/components/levi-api";
import { Badge } from "@/components/ds";
import type { Formats } from "./types";

/** What LEVI converts from and to (the format registry), with the evidence
 * behind each entry and the known gaps. */
export function FormatsTable() {
  const { t } = useLocale();
  const [formats, setFormats] = useState<Formats | null>(null);
  useEffect(() => {
    leviApi<Formats>("convert/formats")
      .then(setFormats)
      .catch(() => {});
  }, []);
  if (!formats) return null;
  const outputs = Object.fromEntries(formats.outputs.map((o) => [o.id, o]));
  return (
    <details className="pg-mt-4">
      <summary className="pg-small">
        <T>Supported formats</T>
      </summary>
      <div className="ds-table-wrap pg-gap-top">
        <table className="ds-table">
          <thead>
            <tr>
              <th>
                <T>Input</T>
              </th>
              <th>
                <T>Exports to</T>
              </th>
              <th>
                <T>Evidence</T>
              </th>
            </tr>
          </thead>
          <tbody>
            {formats.inputs.map((input) => (
              <tr key={input.id}>
                <td>
                  {t(input.label)}
                  <p className="pg-small">{t(input.description)}</p>
                </td>
                <td>
                  {(formats.matrix[input.id] ?? [])
                    .map((id) => t(outputs[id]?.label ?? id))
                    .join(" · ") || "—"}
                </td>
                <td>{t(input.evidence)}</td>
              </tr>
            ))}
            {formats.unsupported.map((gap) => (
              <tr key={gap.id}>
                <td>{t(gap.label)}</td>
                <td>
                  <Badge tone="danger">
                    <T>unsupported</T>
                  </Badge>
                  <p className="pg-small">{t(gap.reason)}</p>
                </td>
                <td className="pg-small">{t(gap.workaround)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}
