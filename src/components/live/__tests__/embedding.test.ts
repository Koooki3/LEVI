import { describe, expect, test } from "bun:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import { DatasetCard } from "../dataset-card";
import {
  datasetLinks,
  disabledText,
  isLiveWorkspace,
  offersTrainingPool,
} from "../embedding";
import type { DatasetDetail, DatasetRow } from "../types";

const row: DatasetRow = {
  episodes: 2,
  pending: 0,
  annotating: 0,
  done: 2,
  failed: 0,
};
const detail = (over: Partial<DatasetDetail> = {}): DatasetDetail => ({
  enabled: true,
  name: "g__t",
  repo_id: "local/g__t",
  demos: [],
  ...over,
});

function card(d: DatasetDetail, state?: string) {
  return renderToStaticMarkup(
    createElement(DatasetCard, {
      name: "g__t",
      row: { ...row, state, awaiting: state ? "plan" : undefined },
      entry: { data: d, error: "", at: 0, signature: "" },
      fault: null,
      open: true,
      onToggle: () => {},
      workerPhase: null,
      filter: "latest",
      onFilter: () => {},
      nowSeconds: 0,
      onChanged: () => {},
    }),
  );
}

describe("the live page in the product LEVI", () => {
  test("says why there is nothing to show, in both languages", () => {
    const reasons = [
      undefined,
      "not_configured",
      "not_live",
      "product_workspace",
    ] as const;
    const titles = new Set(reasons.map((r) => disabledText(r).title));
    expect(titles.size).toBe(3);
    for (const reason of reasons) {
      const { title, body } = disabledText(reason);
      for (const key of [title, body]) {
        expect((en as Record<string, string>)[key]).toBe(key);
        expect((zh as Record<string, string>)[key]).toBeTruthy();
      }
    }
    expect(disabledText(undefined)).toEqual(disabledText("not_configured"));
  });

  test("the live workspace's own LEVI links its viewer and review here", () => {
    expect(datasetLinks(detail({ embedded: false }))).toEqual({
      viewer: { href: "/local/g__t", external: false },
      review: { href: "/workbench", external: false },
    });
    expect(datasetLinks(detail({ repo_id: null })).viewer).toBeNull();
    expect(datasetLinks(undefined)).toEqual({ viewer: null, review: null });
  });

  test("the product LEVI never links its own viewer for a live dataset", () => {
    expect(datasetLinks(detail({ embedded: true, live_ui: null }))).toEqual({
      viewer: null,
      review: null,
    });
    expect(
      datasetLinks(
        detail({ embedded: true, live_ui: "http://127.0.0.1:7880/" }),
      ),
    ).toEqual({
      viewer: { href: "http://127.0.0.1:7880/local/g__t", external: true },
      review: { href: "http://127.0.0.1:7880/workbench", external: true },
    });
  });

  test("the product links its own read-only viewer when it links the live workspace", () => {
    // The viewer is local to the product LEVI, whether or not the service
    // runs a page of its own; the review stays on that page.
    expect(
      datasetLinks(
        detail({
          embedded: true,
          live_ui: null,
          linked_repo_id: "local/live.g__t",
        }),
      ),
    ).toEqual({
      viewer: { href: "/local/live.g__t", external: false },
      review: null,
    });
    expect(
      datasetLinks(
        detail({
          embedded: true,
          live_ui: "http://127.0.0.1:7880/",
          linked_repo_id: "local/live.g__t",
        }),
      ),
    ).toEqual({
      viewer: { href: "/local/live.g__t", external: false },
      review: { href: "http://127.0.0.1:7880/workbench", external: true },
    });
    // Without a link nothing changes (null, or no field at all).
    expect(
      datasetLinks(
        detail({
          embedded: true,
          live_ui: "http://127.0.0.1:7880",
          linked_repo_id: null,
        }),
      ).viewer,
    ).toEqual({ href: "http://127.0.0.1:7880/local/g__t", external: true });
    // The live workspace's own LEVI never uses the product's link.
    expect(
      datasetLinks(detail({ embedded: false, linked_repo_id: "local/live.x" }))
        .viewer,
    ).toEqual({ href: "/local/g__t", external: false });
    // Linked, but the live service has no repo_id of its own (never opened).
    expect(
      datasetLinks(
        detail({
          embedded: true,
          live_ui: "http://127.0.0.1:7880",
          repo_id: null,
          linked_repo_id: "local/live.g__t",
        }),
      ).viewer,
    ).toEqual({ href: "/local/live.g__t", external: false });
  });

  test("the card of a linked dataset opens the product's viewer and drops the --ui hint", () => {
    const html = card(
      detail({
        embedded: true,
        live_ui: null,
        linked_repo_id: "local/live.g__t",
      }),
    );
    expect(html).toContain('href="/local/live.g__t"');
    expect(html).not.toContain('target="_blank"');
    expect(html).not.toContain("levi live start --ui");
    expect(html).toContain("Open in the viewer");
    // No view: the action explains that the view is not ready. A separate
    // live UI is no longer required to register and prepare captures.
    expect(card(detail({ embedded: true, live_ui: null }))).toContain(
      "The dataset viewer is not ready yet.",
    );
    // With a page of its own, the review link is that page's, outside.
    const both = card(
      detail({
        embedded: true,
        live_ui: "http://127.0.0.1:7880",
        linked_repo_id: "local/live.g__t",
      }),
    );
    expect(both).toContain('href="/local/live.g__t"');
    expect(both).toContain('href="http://127.0.0.1:7880/workbench"');
  });

  test("the card in the product says where the viewer is", () => {
    const html = card(detail({ embedded: true, live_ui: null }));
    expect(html).not.toContain('href="/local/g__t"');
    expect(html).not.toContain('href="/workbench"');
    expect(html).toContain("The dataset viewer is not ready yet.");
    const linked = card(
      detail({ embedded: true, live_ui: "http://127.0.0.1:7880" }),
    );
    expect(linked).toContain('href="http://127.0.0.1:7880/local/g__t"');
    expect(linked).toContain('target="_blank"');
    const own = card(detail({ embedded: false }));
    expect(own).toContain('href="/local/g__t"');
    expect(own).toContain('href="/workbench"');
  });

  test("a plan waiting for a person says where to approve it", () => {
    const html = card(
      detail({ embedded: true, live_ui: null }),
      "awaiting_approval",
    );
    expect(html).toContain("--auto-approve");
    const own = card(detail({ embedded: false }), "awaiting_approval");
    expect(own).not.toContain("live workspace&#x27;s own page");
  });

  test("only the product LEVI offers the training pool", () => {
    expect(offersTrainingPool(true, true)).toBe(true); // product, live found
    expect(offersTrainingPool(false, null)).toBe(true); // product, no live
    expect(offersTrainingPool(null, null)).toBe(true); // not known / failed / SSR
    expect(offersTrainingPool(true, null)).toBe(true);
    expect(isLiveWorkspace(null, null)).toBe(false); // the pool page shows the pool
    expect(isLiveWorkspace(true, false)).toBe(true);
    expect((zh as Record<string, string>)["Live workspace"]).toBeTruthy();
    expect(offersTrainingPool(true, false)).toBe(false); // the live workspace
  });

  test("every new sentence is in both catalogs", () => {
    for (const key of [
      "The training pool is in the product LEVI",
      "The episodes' viewer and their review are on the live workspace's own page: start the service with `levi live start --ui` to open them.",
      "That is the live workspace's own page (Conversion & review below), not this LEVI.",
      "That is the live workspace's own page, not this LEVI: start the service with `levi live start --ui` to open it, or with --auto-approve to let it approve its own plans.",
      "Opens read-only in this LEVI; label and review it where the live service keeps it.",
    ]) {
      expect((en as Record<string, string>)[key]).toBe(key);
      expect((zh as Record<string, string>)[key]).toBeTruthy();
    }
  });
});
