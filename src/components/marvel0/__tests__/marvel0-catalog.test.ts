import { describe, expect, test } from "bun:test";
import { readFileSync } from "fs";
import { join } from "path";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";

// The view model and the components name catalogue keys as plain strings
// (a key is looked up by `t(card.title)`, not written in a `t("…")` call), so
// the catalogue test does not see them. This one reads the sources.
const src = join(import.meta.dir, "../../..");
const SOURCES = [
  "components/marvel0/marvel0-logic.ts",
  "components/marvel0/marvel0-card.tsx",
  "components/marvel0/marvel0-say.ts",
  "components/marvel0/use-marvel0-poll.ts",
  "app/marvel_workstation/page.tsx",
  // The action cards.
  "components/marvel0/action-parts.tsx",
  "components/marvel0/catalog-logic.ts",
  "components/marvel0/command-logic.ts",
  "components/marvel0/command-panel.tsx",
  "components/marvel0/deploy-logic.ts",
  "components/marvel0/deploy-parts.tsx",
  "components/marvel0/deploy-panel.tsx",
  "components/marvel0/marvel0-actions.tsx",
  "components/marvel0/marvel0-failure.ts",
  // The arm stack: the passive card of the S4-0 module, and the controls.
  "components/marvel0/stack-status-logic.ts",
  "components/marvel0/stack-evidence.tsx",
  "components/marvel0/stack-act-logic.ts",
  "components/marvel0/stack-dialog-logic.ts",
  "components/marvel0/stack-dialog.tsx",
  "components/marvel0/confirm-window.tsx",
  "components/marvel0/stack-controls.tsx",
  "components/marvel0/stack-panel.tsx",
  "components/marvel0/stack-sequence.tsx",
  "components/marvel0/stack-sequence-logic.ts",
  "components/marvel0/use-stack-actions.ts",
  "components/marvel0/use-stack-poll.ts",
  "components/marvel0/ctl-failure.ts",
  "components/marvel0/ctl-text.ts",
  "components/marvel0/live-age.tsx",
  // The chain at the top.
  "components/marvel0/chain-logic.ts",
  "components/marvel0/chain-strip.tsx",
  // The hosted client.
  "components/marvel0/run-logic.ts",
  "components/marvel0/run-panel.tsx",
  "components/marvel0/run-start.tsx",
  "components/marvel0/use-run-actions.ts",
  "components/marvel0/use-run-poll.ts",
  "components/marvel0/selection.ts",
  // The card to the offline 3D preview.
  "components/marvel0/studio-link.tsx",
  // The navigation entry's word after its label.
  "components/shell/commands.ts",
];
// A key is named as a whole ("marvel0.ctl.estop") or as the start of a family
// the code finishes with a backend code or a state ("marvel0.ctl.block.",
// "marvel0.cmd.follow_", or a template `marvel0.ctl.state.${state}`): the
// members of a family are checked against what produces them in
// ctl-catalog.test.ts.
const named = new Set<string>();
const families: string[] = [];
for (const file of SOURCES)
  for (const match of readFileSync(join(src, file), "utf8").matchAll(
    /[`"](marvel0\.[a-z0-9_.A-Z]+)(?:[`"]|\$\{)/g,
  )) {
    const name = match[1];
    (/[._]$/.test(name) ? families : []).push(name);
    if (!/[._]$/.test(name)) named.add(name);
  }
const inFamily = (key: string) => families.some((p) => key.startsWith(p));

const both = (key: string) => key in en && key in zh;

describe("the marvel0's catalogue entries", () => {
  test("every key the code names is in both languages", () => {
    expect(named.size).toBeGreaterThan(100);
    expect([...named].filter((key) => !both(key))).toEqual([]);
  });

  test("no marvel0.* entry is left over", () => {
    const entries = Object.keys(en).filter((key) => key.startsWith("marvel0."));
    expect(entries.filter((key) => !named.has(key) && !inFamily(key))).toEqual(
      [],
    );
    expect(Object.keys(zh).filter((key) => key.startsWith("marvel0."))).toEqual(
      entries,
    );
  });

  test("the page, its navigation entry and its chord have both languages", () => {
    for (const key of ["marvel0", "Go to: marvel0"])
      expect(both(key)).toBe(true);
    // The page is called marvel_workstation (it was MARVEL0 until 2026-10-21);
    // the keys marvel0.* are identifiers and keep their names.
    expect((en as Record<string, string>)["marvel0"]).toBe(
      "marvel_workstation",
    );
    expect((zh as Record<string, string>)["marvel0"]).toBe(
      "marvel_workstation",
    );
    expect(en["Go to: marvel0"]).toBe("Go to: marvel_workstation");
    expect(zh["Go to: marvel0"]).toBe("跳转：marvel_workstation");
    expect(Object.keys(en)).not.toContain("marvel0.nav.hint");
  });

  test("the scope texts say the backend runs read-only queries, in both languages", () => {
    for (const catalogue of [en, zh] as Array<Record<string, string>>)
      for (const key of ["marvel0.scope.read_only", "marvel0.gpu.hint"]) {
        expect(catalogue[key]).toContain("nvidia-smi");
        expect(catalogue[key]).toContain("systemctl --user list-units");
      }
  });

  test("no literal 'unknown' stands in for a missing value: the catalogue says it", () => {
    for (const file of SOURCES) {
      const code = readFileSync(join(src, file), "utf8");
      expect(code.match(/(\?\?|\|\|)\s*"unknown"/g) ?? []).toEqual([]);
    }
  });

  test("Chinese text keeps the placeholders of the English text", () => {
    for (const key of Object.keys(en).filter((k) => k.startsWith("marvel0."))) {
      const placeholders = (text: string) =>
        [...text.matchAll(/\{(\w+)\}/g)].map((match) => match[1]).sort();
      expect(placeholders((zh as Record<string, string>)[key])).toEqual(
        placeholders((en as Record<string, string>)[key]),
      );
    }
  });
});
