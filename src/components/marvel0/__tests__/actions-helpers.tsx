// Helpers of the tests of the marvel0's action cards: a Marvel0Api made of
// canned answers (each may be a function, to change over time), the calls it
// saw, and the few DOM gestures the cards need.
import { act } from "react";
import {
  errorOf,
  type ApiFailure,
  type ApiResult,
  type Marvel0Api,
} from "../deploy-api";
import { modelsAnswer, siteAnswer } from "./catalog-fixtures";
import { idle, logAnswer } from "./deploy-fixtures";
import { preview } from "./command-fixtures";

export const ok = (data: unknown, status = 200): ApiResult => ({
  ok: true,
  status,
  data,
});
export const refused = (
  status: number,
  code: string,
  extra: Record<string, unknown> = {},
  message = "The server's English sentence",
): ApiFailure => errorOf(status, { detail: { code, message, ...extra } });
export const nothing = (message = "fetch failed"): ApiFailure => ({
  ok: false,
  status: 0,
  code: "network",
  message,
  extra: {},
  field: null,
  fields: [],
});

type Answer = ApiResult | (() => ApiResult | Promise<ApiResult>);
const resolve = (answer: Answer) =>
  typeof answer === "function" ? answer() : answer;

export interface Canned {
  models?: Answer;
  site?: Answer;
  deploy?: Answer;
  log?: Answer;
  startDeploy?: Answer;
  stopDeploy?: Answer;
  preview?: Answer;
}

export interface Calls {
  models: number;
  site: number;
  deploy: number;
  /** The abort signal of each read of the state. */
  signals: Array<AbortSignal | undefined>;
  log: number;
  startDeploy: Array<Record<string, unknown>>;
  stopDeploy: Array<Record<string, unknown>>;
  preview: Array<Record<string, unknown>>;
}

/** A client that answers from `canned` and records what it was asked. A write
 * that is not canned fails the test: nothing here may POST by accident. */
export function fakeApi(canned: Canned = {}): {
  api: Marvel0Api;
  calls: Calls;
} {
  const calls: Calls = {
    models: 0,
    site: 0,
    deploy: 0,
    signals: [],
    log: 0,
    startDeploy: [],
    stopDeploy: [],
    preview: [],
  };
  const write =
    (name: "startDeploy" | "stopDeploy" | "preview") =>
    async (body: unknown) => {
      calls[name].push(body as Record<string, unknown>);
      const answer = canned[name];
      if (!answer) throw new Error(`unexpected ${name}`);
      return resolve(answer);
    };
  const api: Marvel0Api = {
    models: async () => {
      calls.models += 1;
      return resolve(canned.models ?? ok(modelsAnswer()));
    },
    site: async () => {
      calls.site += 1;
      return resolve(canned.site ?? ok(siteAnswer()));
    },
    deploy: async (signal) => {
      calls.deploy += 1;
      calls.signals.push(signal);
      return resolve(canned.deploy ?? ok(idle()));
    },
    log: async () => {
      calls.log += 1;
      return resolve(canned.log ?? ok(logAnswer()));
    },
    startDeploy: write("startDeploy"),
    stopDeploy: write("stopDeploy"),
    preview: write("preview"),
  };
  return { api, calls };
}

export const previewOk = () => ok(preview());

export const button = (host: HTMLElement, text: string | RegExp) =>
  Array.from(host.querySelectorAll("button")).find((node) =>
    typeof text === "string"
      ? node.textContent?.trim() === text
      : text.test(node.textContent ?? ""),
  ) as HTMLButtonElement | undefined;

/** Types into a text field the way a person does: React reads the value on
 * the input event. */
export async function type(
  field: HTMLInputElement | HTMLTextAreaElement | null,
  text: string,
) {
  if (!field) throw new Error("no field to type into");
  const proto =
    field instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype
      : HTMLInputElement.prototype;
  await act(async () => field.focus());
  await act(async () => {
    Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(field, text);
    field.dispatchEvent(new Event("input", { bubbles: true }));
    // React reads the value of the focused field on keyup (as in shell-dom).
    field.dispatchEvent(
      new KeyboardEvent("keyup", { bubbles: true, key: "e" }),
    );
  });
}

export async function choose(select: HTMLSelectElement | null, value: string) {
  if (!select) throw new Error("no select");
  await act(async () => {
    select.value = value;
    select.dispatchEvent(new Event("change", { bubbles: true }));
  });
}

export async function tick(times = 3, ms = 0) {
  for (let i = 0; i < times; i += 1)
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, ms));
    });
}

/** The labelled control of a Field: found by the text of its label. */
export function field<T extends Element = HTMLInputElement>(
  host: ParentNode,
  label: string | RegExp,
): T | null {
  const found = Array.from(host.querySelectorAll("label")).find((node) =>
    typeof label === "string"
      ? node.textContent?.includes(label)
      : label.test(node.textContent ?? ""),
  );
  if (!found) return null;
  const id = found.getAttribute("for");
  if (id) return host.querySelector<T>(`[id="${id}"]`);
  return found.querySelector<T>("input, select");
}
