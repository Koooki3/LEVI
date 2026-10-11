"use client";
import { Copy } from "lucide-react";
import { useLocale } from "@/components/levi-locale";
import { Button } from "./Button";
import { useToast } from "./Toast";

/** Short summaries retain a keyboard-accessible full text and exact copy. */
export function ExpandableText({
  text,
  mono = false,
  previewLength = 160,
}: {
  text: string;
  mono?: boolean;
  /** A shorter preview for dense pickers; complete text and copy stay intact. */
  previewLength?: number;
}) {
  const { t } = useLocale();
  const toast = useToast();
  const content = (value: string) =>
    mono ? <code>{value}</code> : <span>{value}</span>;
  if (text.length <= 180) return content(text);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      toast.show({ title: t("Text copied") });
    } catch {
      toast.show({
        tone: "danger",
        title: t("Could not copy the text"),
        description: t("Select it and copy it by hand."),
      });
    }
  };
  return (
    <div className="ds-long-text">
      <details>
        <summary>
          <span className="ds-long-text__preview">
            {content(text.slice(0, previewLength))}…{" "}
          </span>
          <span className="ds-long-text__label">{t("Full text")}</span>
        </summary>
        <div className="ds-long-text__body">{content(text)}</div>
        <Button size="sm" icon={Copy} onClick={copy}>
          {t("Copy full text")}
        </Button>
      </details>
    </div>
  );
}
