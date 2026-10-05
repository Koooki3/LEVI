"use client";
/**
 * Small building blocks of the Agent Workbench content (the drawer's four
 * sections). The drawer's own shell is the frame's; everything inside it is
 * built from the design system plus these few pieces: a disclosure, a row of
 * actions, a help line and a button whose "why not" is written beside it.
 * Styles: pages-ui/agent-content.css (`ag-*`).
 */
import "@/components/pages-ui/agent-content.css";
import "@/components/pages-ui/pages.css";
import { useId, type ReactNode } from "react";
import { ChevronRight, type LucideIcon } from "lucide-react";
import { Button, Icon, type ButtonProps } from "@/components/ds";

/**
 * A native `<details>` with the design system's look: a chevron that turns
 * when it opens (no turn under reduced motion) and a framed body. Pass `open`
 * to control it from the parent, `defaultOpen` to start open.
 */
export function Disclosure({
  summary,
  children,
  open,
  defaultOpen,
  icon,
  className,
}: {
  summary: ReactNode;
  children: ReactNode;
  open?: boolean;
  defaultOpen?: boolean;
  icon?: LucideIcon;
  className?: string;
}) {
  return (
    <details
      className={["ag-disclosure", className ?? ""].filter(Boolean).join(" ")}
      {...(open !== undefined ? { open } : {})}
      {...(open === undefined && defaultOpen ? { open: true } : {})}
    >
      <summary className="ag-disclosure__summary ds-focus">
        <Icon icon={ChevronRight} className="ag-disclosure__chevron" />
        {icon && <Icon icon={icon} />}
        <span>{summary}</span>
      </summary>
      <div className="ag-disclosure__body">{children}</div>
    </details>
  );
}

/** A wrapping row of buttons. */
export function Actions({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={["ag-actions", className ?? ""].filter(Boolean).join(" ")}>
      {children}
    </div>
  );
}

/** A line of help under a control or a block. */
export function Hint({
  children,
  id,
  className,
}: {
  children: ReactNode;
  id?: string;
  className?: string;
}) {
  return (
    <p
      id={id}
      className={["ag-hint", className ?? ""].filter(Boolean).join(" ")}
    >
      {children}
    </p>
  );
}

/**
 * A button that can be blocked for a reason a person can fix. While `reason`
 * is set the button is disabled and the reason is written under it (and
 * linked with `aria-describedby`), so a disabled control never leaves the
 * question "why" to a tooltip. Without a reason it is a plain Button.
 */
export function GatedButton({
  reason,
  disabled,
  ...rest
}: ButtonProps & { reason?: ReactNode | null }) {
  const id = useId();
  if (!reason) return <Button disabled={disabled} {...rest} />;
  const { "aria-describedby": more, ...props } = rest;
  return (
    <span className="ag-gated">
      <Button
        disabled
        {...props}
        aria-describedby={[id, more].filter(Boolean).join(" ")}
      />
      <span id={id} className="ag-why">
        {reason}
      </span>
    </span>
  );
}

/** A square tile with an icon: the avatar of a connection card. */
export function IconTile({ icon }: { icon: LucideIcon }) {
  return (
    <span className="ag-tile" aria-hidden="true">
      <Icon icon={icon} size="md" />
    </span>
  );
}

/**
 * The head of a connection card: tile, name, a second line and a status
 * (a Badge) on the right.
 */
export function ConnectionHead({
  icon,
  title,
  subtitle,
  status,
}: {
  icon: LucideIcon;
  title: ReactNode;
  subtitle?: ReactNode;
  status?: ReactNode;
}) {
  return (
    <div className="ag-conn-head">
      <IconTile icon={icon} />
      <div className="ag-conn-head__text">
        <strong className="ag-conn-head__title">{title}</strong>
        {subtitle && <span className="ag-conn-head__sub">{subtitle}</span>}
      </div>
      {status}
    </div>
  );
}
