import { describe, expect, test } from "bun:test";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import {
  isLinkedDataset,
  isLinkedRefusal,
  isLinkedRepo,
  LINKED_NOTICE,
  LINKED_READ_ONLY,
  LINKED_REASON,
  writeFailure,
} from "../linkedDataset";

describe("which datasets are the live workspace's, read-only here", () => {
  test("by the id alone: only a local dataset named live.<name>", () => {
    expect(isLinkedRepo("local/live.run1")).toBe(true);
    expect(isLinkedRepo("local/live.")).toBe(true);
    expect(isLinkedRepo("local/run1")).toBe(false);
    expect(isLinkedRepo("local/my.live.run1")).toBe(false);
    // A Hub dataset is never one, whatever its name.
    expect(isLinkedRepo("lerobot/live.run1")).toBe(false);
    expect(isLinkedRepo("live.run1")).toBe(false);
    expect(isLinkedRepo(null)).toBe(false);
    expect(isLinkedRepo(undefined)).toBe(false);
    expect(isLinkedRepo("")).toBe(false);
  });

  test("the catalog entry decides once it has answered; the id guesses before", () => {
    const linked = {
      linked: {
        kind: "live" as const,
        source: "run1",
        workspace: "levi-live-ws",
        readonly: true as const,
      },
    };
    expect(isLinkedDataset("local/live.run1", linked)).toBe(true);
    // The service says it is not: a dataset of the user's own named live.x.
    expect(isLinkedDataset("local/live.run1", {})).toBe(false);
    expect(isLinkedDataset("local/run1", linked)).toBe(true);
    // No answer yet: the safe side, nothing is written meanwhile.
    expect(isLinkedDataset("local/live.run1", null)).toBe(true);
    expect(isLinkedDataset("local/run1", undefined)).toBe(false);
  });

  test("the service's refusal is recognised however it is wrapped", () => {
    expect(isLinkedRefusal(LINKED_READ_ONLY)).toBe(true);
    expect(isLinkedRefusal(`{"detail":"${LINKED_READ_ONLY}"}`)).toBe(true);
    expect(isLinkedRefusal(`Error: ${LINKED_READ_ONLY}`)).toBe(true);
    expect(isLinkedRefusal("save atoms: 403")).toBe(false);
    expect(isLinkedRefusal(null)).toBe(false);
    expect(
      writeFailure(`{"detail":"${LINKED_READ_ONLY}"}`, "Save failed"),
    ).toBe(LINKED_READ_ONLY);
    expect(writeFailure("disk full", "Save failed")).toBe("Save failed");
  });

  test("the sentences are in both catalogues (the service's own sentence word for word)", () => {
    expect(LINKED_READ_ONLY).toBe(
      "This dataset belongs to the live evaluation workspace and is read-only here; review and label it where the live service keeps it",
    );
    for (const key of [
      LINKED_READ_ONLY,
      LINKED_REASON,
      LINKED_NOTICE,
      "Live evaluation · read-only",
    ]) {
      expect((en as Record<string, string>)[key]).toBe(key);
      expect((zh as Record<string, string>)[key]).toBeTruthy();
    }
    expect((zh as Record<string, string>)[LINKED_NOTICE]).toContain("只读");
    expect((zh as Record<string, string>)[LINKED_NOTICE]).toContain("实时服务");
  });
});
