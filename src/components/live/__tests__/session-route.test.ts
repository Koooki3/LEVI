import { describe, expect, test } from "bun:test";
import {
  findSession,
  identityOf,
  parseSessionIdentity,
  sessionHref,
  statsSessionHref,
} from "../session-route";
import type { LiveSession } from "../types";
const base: LiveSession = {
  root: "/captures/模型 %1",
  group: "model",
  task_folder: "task",
  session_id: "session1",
  run_id: "run1",
  dataset: "model__task",
  state: "finished",
};
describe("live session bookmarks", () => {
  test("client navigation's encoded segment and direct-load JSON both work", () => {
    const encoded = sessionHref(base)!.split("/sessions/")[1];
    expect(parseSessionIdentity(encoded)).toEqual(identityOf(base));
    expect(parseSessionIdentity(decodeURIComponent(encoded))).toEqual(
      identityOf(base),
    );
    expect(parseSessionIdentity(encodeURIComponent(encoded))).toBeNull();
  });
  test("round trips unicode, percent signs and slashes exactly once", () => {
    const href = sessionHref(base)!;
    const identity = parseSessionIdentity(
      decodeURIComponent(href.split("/sessions/")[1]),
    );
    expect(identity).toEqual(identityOf(base));
    expect(findSession(identity!, [base])).toBe(base);
  });
  test("same task in a different root or run never matches", () => {
    const id = identityOf(base)!;
    expect(findSession(id, [{ ...base, root: "other" }])).toBeNull();
    expect(findSession(id, [{ ...base, session_id: "another" }])).toBeNull();
    expect(findSession(id, [{ ...base, run_id: "another" }])).toBeNull();
  });
  test("ambiguous snapshots remain unselected", () =>
    expect(findSession(identityOf(base)!, [base, { ...base }])).toBeNull());
  test("no stable identifier produces no bookmark", () =>
    expect(
      sessionHref({ ...base, session_id: undefined, run_id: undefined }),
    ).toBeNull());
  test("run-only old records still link", () =>
    expect(sessionHref({ ...base, session_id: undefined })).not.toBeNull());
  test("history bookmarks match dataset and run, not the current unrelated run", () => {
    const href = statsSessionHref({
      dataset: base.dataset,
      session: base.run_id,
    })!;
    const id = parseSessionIdentity(
      decodeURIComponent(href.split("/sessions/")[1]),
    )!;
    expect(findSession(id, [base])).toBe(base);
    expect(findSession(id, [{ ...base, run_id: "later" }])).toBeNull();
  });
  test("missing snapshot is legitimate history", () =>
    expect(findSession(identityOf(base)!, [])).toBeNull());
  test("invalid and incomplete identities are refused", () => {
    for (const raw of [
      "bad",
      "null",
      "{}",
      JSON.stringify({ ...identityOf(base), root: 2 }),
      JSON.stringify({ ...identityOf(base), id: "", run: "" }),
      JSON.stringify({ ...identityOf(base), group: "", dataset: "" }),
    ])
      expect(parseSessionIdentity(raw)).toBeNull();
  });
  test("unscoped statistics cannot produce a link", () => {
    expect(statsSessionHref({ session: "run" })).toBeNull();
    expect(statsSessionHref({ dataset: "d" })).toBeNull();
  });
});
