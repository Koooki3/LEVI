/**
 * Window events the global frame listens to. The header, the command palette
 * and older components (which dispatched these names before the frame
 * existed) all open the same panels through them.
 */
export const SHELL_EVENTS = {
  /** Open or close the Agent Workbench drawer. */
  agentToggle: "levi-agent-toggle",
  /** Open the Agent Workbench on "Accounts & connections". */
  agentConnections: "levi-agent-connections",
  /** Sent by the drawer when it opens or closes: `{ detail: { open } }`. */
  agentState: "levi-agent-state",
  /** A keyboard jump to a page: `{ detail: href }`; the frame confirms first
   * when there is unsaved work (app-frame.tsx). */
  go: "levi-go",
} as const;

export function requestGo(href: string): void {
  window.dispatchEvent(new CustomEvent(SHELL_EVENTS.go, { detail: href }));
}

export function toggleAgentWorkbench(): void {
  window.dispatchEvent(new Event(SHELL_EVENTS.agentToggle));
}

export function openAgentConnections(): void {
  window.dispatchEvent(new CustomEvent(SHELL_EVENTS.agentConnections));
}
