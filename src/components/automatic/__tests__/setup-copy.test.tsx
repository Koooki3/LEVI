import {
  click,
  render,
  setupDom,
  waitFor,
} from "@/components/ds/__tests__/dom";
import { afterEach, describe, expect, test } from "bun:test";
import { SetupCommand } from "../setup-copy";

setupDom();

const original = Object.getOwnPropertyDescriptor(navigator, "clipboard");
afterEach(() => {
  if (original) Object.defineProperty(navigator, "clipboard", original);
  else delete (navigator as { clipboard?: unknown }).clipboard;
});
const withClipboard = (writeText: (text: string) => Promise<void>) =>
  Object.defineProperty(navigator, "clipboard", {
    value: { writeText },
    configurable: true,
  });
const copyButton = (host: HTMLElement) =>
  host.querySelector("button") as HTMLButtonElement;

describe("the setup command's Copy button", () => {
  test("a copy that worked says Copied", async () => {
    const written: string[] = [];
    withClipboard(async (text) => void written.push(text));
    const { host } = await render(
      <SetupCommand command="python run.py" label="run" />,
    );
    await click(copyButton(host));
    await waitFor(() => copyButton(host).textContent === "Copied");
    expect(written).toEqual(["python run.py"]);
    expect(host.querySelector("[role=alert]")).toBeNull();
  });
  test("a refused copy is said next to the button, not swallowed", async () => {
    withClipboard(async () => {
      throw new Error("denied");
    });
    const { host } = await render(
      <SetupCommand command="python run.py" label="run" />,
    );
    await click(copyButton(host));
    await waitFor(() => host.querySelector("[role=alert]"));
    expect(host.querySelector("[role=alert]")!.textContent).toContain(
      "Could not copy the command",
    );
    expect(copyButton(host).textContent).toBe("Copy");
  });
  test("no clipboard at all is a failure too", async () => {
    Object.defineProperty(navigator, "clipboard", {
      value: undefined,
      configurable: true,
    });
    const { host } = await render(
      <SetupCommand command="python run.py" label="run" />,
    );
    await click(copyButton(host));
    await waitFor(() => host.querySelector("[role=alert]"));
  });
});
