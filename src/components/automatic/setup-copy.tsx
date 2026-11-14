"use client";
import { useEffect, useRef, useState } from "react";
import { Check, ClipboardCopy } from "lucide-react";
import { Button, useToast } from "@/components/ds";
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
  const toast = useToast();
  const [copied, setCopied] = useState(false);
  const [failed, setFailed] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);
  const copy = async () => {
    clearTimeout(timer.current);
    try {
      if (!navigator.clipboard) throw new Error("no clipboard");
      await navigator.clipboard.writeText(command);
      setFailed(false);
      setCopied(true);
      timer.current = setTimeout(() => setCopied(false), 2000);
    } catch {
      // Say it next to the button (and in a toast): a silent failure looks
      // like a copy that worked.
      setCopied(false);
      setFailed(true);
      toast.show({
        tone: "danger",
        title: t("Could not copy the command"),
        description: t("Select it and copy it by hand."),
      });
    }
  };
  return (
    <div className="aw-command">
      <code className="aw-command__text">{command}</code>
      <Button
        size="sm"
        icon={copied ? Check : ClipboardCopy}
        onClick={() => void copy()}
        aria-label={`${t("Copy")}: ${label}`}
      >
        {copied ? t("Copied") : t("Copy")}
      </Button>
      {failed && (
        <span className="ar-muted" role="alert">
          {t("Could not copy the command")}{" "}
          {t("Select it and copy it by hand.")}
        </span>
      )}
    </div>
  );
}
