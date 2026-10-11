import {
  click,
  render,
  setupDom,
  waitFor,
} from "@/components/ds/__tests__/dom";
import { afterEach, describe, expect, test } from "bun:test";
import {
  Fr3BridgeRecipe,
  bridgeRecipePath,
  type Fr3Recipe,
} from "../fr3-bridge-recipe";
setupDom();
const recipe: Fr3Recipe = {
  entrypoint: "python -B -m levi.automatic.fr3_run",
  check_command:
    "python -B -m levi.automatic.fr3_run --binding '<reviewed-binding.json>' --check",
  execute_command_template:
    "python -B -m levi.automatic.fr3_run --execute --expect-plan abc --expect-binding '<binding-sha256-from-check>'",
  plan_sha256: "abc",
  hardware_verified: false,
  manual_review_required: true,
  requires_new_plan: true,
  blockers: [
    "confirmed_initial_state_contract_required",
    "physical_validation_not_performed",
  ],
};
const originalFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = originalFetch;
});
describe("FR3 preparation recipe", () => {
  test("encodes the exact job id or campaign segment without treating a run as a job", () => {
    expect(bridgeRecipePath({ jobId: "root/path %" })).toBe(
      "automatic/jobs/root%2Fpath%20%25/fr3-bridge",
    );
    expect(bridgeRecipePath({ campaignId: "camp", segment: 2 })).toBe(
      "automatic/campaigns/camp/fr3-bridge?segment=2",
    );
  });
  test("loads only on demand with GET and copies the unmodified execution template", async () => {
    const calls: { path: string; method: string }[] = [];
    globalThis.fetch = (async (path: unknown, init: RequestInit) => {
      calls.push({ path: String(path), method: init.method! });
      return new Response(JSON.stringify(recipe));
    }) as typeof fetch;
    const { host } = await render(
      <Fr3BridgeRecipe target={{ jobId: "job-1" }} />,
    );
    expect(calls).toEqual([]);
    await click(host.querySelector("button"));
    await waitFor(() => host.textContent?.includes("Plan digest"));
    expect(calls).toEqual([
      { path: "/api/levi/automatic/jobs/job-1/fr3-bridge", method: "GET" },
    ]);
    expect(host.textContent).toContain("Hardware unverified");
    expect(host.textContent).toContain("Create a new plan");
    expect(host.textContent).toContain(recipe.check_command);
    let copied = "";
    const original = navigator.clipboard.writeText;
    navigator.clipboard.writeText = async (value) => {
      copied = value;
    };
    try {
      await click(
        host.querySelector(
          '[aria-label="Copy: Execution template for manual review"]',
        ),
      );
      expect(copied).toBe(recipe.execute_command_template);
      expect(calls.length).toBe(1);
    } finally {
      navigator.clipboard.writeText = original;
    }
  });
  test("old backend 404 is explained without presenting an execution template", async () => {
    globalThis.fetch = (async () =>
      new Response(JSON.stringify({ detail: "missing" }), {
        status: 404,
      })) as typeof fetch;
    const { host } = await render(
      <Fr3BridgeRecipe target={{ campaignId: "camp", segment: 2 }} />,
    );
    await click(host.querySelector("button"));
    await waitFor(() => host.textContent?.includes("does not provide FR3"));
    expect(host.querySelector(".aw-command")).toBeNull();
  });
  test("changing targets clears old commands before a new read", async () => {
    const { host, rerender } = await render(
      <Fr3BridgeRecipe target={{ jobId: "a" }} load={async () => recipe} />,
    );
    await click(host.querySelector("button"));
    await waitFor(() => host.textContent?.includes("Plan digest"));
    await rerender(
      <Fr3BridgeRecipe target={{ jobId: "b" }} load={async () => recipe} />,
    );
    expect(host.querySelector(".aw-command")).toBeNull();
  });
});
