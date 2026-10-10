"use client";
import { useEffect, useRef, useState } from "react";
import { Check, ClipboardCopy } from "lucide-react";
import { Button } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";

/** A command in a box with a Copy button. It only ever copies: nothing in the
 * interface runs the command. */
export function SetupCommand({
  command,
  label,
}: {
  command: string;
  /** Accessible name of the copy button (which command it copies). */
  label: string;
}) {
  const { t } = useLocale();
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);
  const copy = () => {
    void navigator.clipboard
      ?.writeText(command)
      .then(() => {
        setCopied(true);
        timer.current = setTimeout(() => setCopied(false), 2000);
      })
      .catch(() => {});
  };
  return (
    <div className="aw-command">
      <code className="aw-command__text">{command}</code>
      <Button
        size="sm"
        icon={copied ? Check : ClipboardCopy}
        onClick={copy}
        aria-label={`${t("Copy")}: ${label}`}
      >
        {copied ? t("Copied") : t("Copy")}
      </Button>
    </div>
  );
}
