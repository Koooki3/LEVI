import { afterEach, describe, expect, test } from "bun:test";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { forgetUiToken, uiToken } from "@/utils/serviceToken";

function workspace(key: string): string {
  const directory = mkdtempSync(join(tmpdir(), "levi-core-"));
  writeFileSync(join(directory, "human.key"), key);
  return directory;
}

afterEach(() => {
  forgetUiToken();
  delete process.env.LEVI_CORE_DIR;
  delete process.env.LEVI_UI_TOKEN;
});

describe("uiToken", () => {
  test("follows the service's key file across a restart", () => {
    const directory = workspace("first-key");
    process.env.LEVI_CORE_DIR = directory;
    process.env.LEVI_UI_TOKEN = "captured-at-launch";
    expect(uiToken()).toBe("first-key");

    // The Core restarts and rewrites its key; the proxy must not keep using
    // the value it captured when the frontend started.
    writeFileSync(join(directory, "human.key"), "second-key");
    forgetUiToken();
    expect(uiToken()).toBe("second-key");
  });

  test("falls back to the environment when no key file is configured", () => {
    process.env.LEVI_UI_TOKEN = "from-environment";
    expect(uiToken()).toBe("from-environment");
  });

  test("falls back when the key file cannot be read", () => {
    process.env.LEVI_CORE_DIR = join(tmpdir(), "levi-core-missing-directory");
    process.env.LEVI_UI_TOKEN = "from-environment";
    expect(uiToken()).toBe("from-environment");
  });

  test("ignores surrounding whitespace in the key file", () => {
    process.env.LEVI_CORE_DIR = workspace("  padded-key\n");
    expect(uiToken()).toBe("padded-key");
  });

  test("reports no token rather than an empty one", () => {
    process.env.LEVI_CORE_DIR = workspace("   ");
    expect(uiToken()).toBeUndefined();
  });
});
