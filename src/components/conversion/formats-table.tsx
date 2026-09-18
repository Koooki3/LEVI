"use client";
import { useEffect, useState } from "react";
import { T, useLocale } from "@/components/levi-locale";
import { leviApi } from "@/components/levi-api";
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
    <details className="mt-4">
      <summary className="cursor-pointer text-xs">
        <T>Supported formats</T>
      </summary>
      <table className="levi-table">
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
                <p className="text-xs">{t(input.description)}</p>
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
                <span className="levi-status fail">
                  <T>unsupported</T>
                </span>
                <p className="text-xs">{t(gap.reason)}</p>
              </td>
              <td className="text-xs">{t(gap.workaround)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </details>
  );
}
