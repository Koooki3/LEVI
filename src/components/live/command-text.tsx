"use client";
/**
 * Text that names commands between backticks (`levi live start`): the
 * commands are drawn as <code>, not with the backticks showing, and the
 * first one can be copied with a button (a command is something to paste
 * into a terminal, not to retype).
 */
import { Fragment, type ReactNode } from "react";
import { Copy } from "lucide-react";
import { Button, useToast } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";

/** The commands between backticks, in order. */
export function commandsIn(text: string): string[] {
  return [...text.matchAll(/`([^`]+)`/g)].map((match) => match[1].trim());
}

/** The text with each backticked command as <code>. */
export function CommandText({ text }: { text: string }): ReactNode {
  const parts = text.split(/`([^`]+)`/);
  return (
    <>
      {parts.map((part, index) =>
        index % 2 === 1 ? (
          <code key={index}>{part}</code>
        ) : (
          <Fragment key={index}>{part}</Fragment>
        ),
      )}
    </>
  );
}

/** Copies `command` to the clipboard and says so (or says it could not). */
export function CopyCommandButton({ command }: { command: string }) {
  const { t } = useLocale();
  const toast = useToast();
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(command);
      toast.show({ title: t("Command copied"), description: command });
    } catch {
      toast.show({
        tone: "danger",
        title: t("Could not copy the command"),
        description: t("Select it and copy it by hand."),
      });
    }
  };
  return (
    <Button size="sm" icon={Copy} onClick={copy}>
      {t("Copy command")}
    </Button>
  );
}
