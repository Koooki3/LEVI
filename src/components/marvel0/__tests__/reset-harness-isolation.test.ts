// The offline reset rehearsal card (`physical_execution` false) is kept apart from
// the real-robot routes of the autonomy page, in code and by route: the takeover
// before the reset policy (GRASP_ASSIST, subtask.grasp_assist) moves a real arm
// through terminal D's keys, and nothing of the rehearsal may reach them.
import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { createAutonomyApi } from "../autonomy-api";
import { createResetHarnessApi } from "../reset-harness-api";

const HERE = join(import.meta.dir, "..");
const source = (name: string) => readFileSync(join(HERE, name), "utf8");
const REHEARSAL = [
  "reset-harness-api.ts",
  "reset-harness-card.tsx",
  "reset-harness-doc.ts",
];
// What the rehearsal's own files may import (besides React and the design system).
const ALLOWED = new Set([
  "./deploy-api",
  "./deploy-logic",
  "./marvel0-logic",
  "./use-polled",
  "./reset-harness-api",
  "./reset-harness-doc",
  "@/components/ds",
  "@/components/levi-locale",
  "@/components/pages-ui/feedback",
  "react",
]);
// The real-robot side: the autonomy page's routes, keys, run client and the
// hosted client's manual/run routes.
const REAL_ROBOT = [
  "autonomy-api",
  "autonomy-doc",
  "autonomy-keys",
  "autonomy-run-card",
  "use-autonomy",
  "run-api",
  "aeri-run",
  "aeri/run",
  "/key",
  "KEY_IDS",
  "GRASP_ASSIST",
  "grasp_assist",
  // the takeover's buttons and their table (autonomy-keys.tsx, autonomy-doc.ts)
  "assist_",
  "ASSIST_KEY_IDS",
  "TakeoverBlock",
  "isAssistKey",
];

describe("the offline rehearsal card is separate from the real-robot routes", () => {
  test("its files import nothing from the real-robot side", () => {
    for (const file of REHEARSAL) {
      const imports = [...source(file).matchAll(/from\s+"([^"]+)"/g)].map(
        (m) => m[1],
      );
      for (const found of imports) {
        expect(ALLOWED.has(found)).toBe(true);
      }
    }
  });
  test("its files never name a real-robot module, route or key", () => {
    for (const file of REHEARSAL) {
      const text = source(file);
      for (const word of REAL_ROBOT) {
        expect(text.includes(word)).toBe(false);
      }
    }
  });
  test("every URL it calls is under its own route and none is a route of the real robot", async () => {
    const rehearsal: string[] = [];
    const real: string[] = [];
    const answer = async () => new Response(JSON.stringify({}));
    const offline = createResetHarnessApi({
      fetchFn: async (url) => {
        rehearsal.push(url);
        return answer();
      },
    });
    const robot = createAutonomyApi({
      fetchFn: async (url) => {
        real.push(url);
        return answer();
      },
    });
    await offline.catalog();
    await offline.run("offline-1");
    await offline.start({
      request_id: "r1",
      scenario_id: "normal_random",
      seed: 1,
    });
    await offline.stop("offline-1", { request_id: "r2" });
    await robot.ready();
    await robot.run();
    await robot.key({});
    await robot.stop({});
    expect(rehearsal.length).toBe(4);
    for (const url of rehearsal) {
      expect(url.startsWith("/api/levi/marvel0/reset-harness")).toBe(true);
      expect(url.includes("aeri")).toBe(false);
      expect(real.includes(url)).toBe(false);
    }
    for (const url of real) {
      expect(url.startsWith("/api/levi/marvel0/reset-harness")).toBe(false);
    }
    // The rehearsal client has no method that could press a key or start a run.
    expect(Object.keys(offline).sort()).toEqual([
      "catalog",
      "run",
      "start",
      "stop",
    ]);
  });
  test("its documents are always physical_execution false", () => {
    const text = source("reset-harness-doc.ts");
    expect(text).toContain("physical_execution: false");
    expect(text).toContain("data.physical_execution !== false");
  });
});
